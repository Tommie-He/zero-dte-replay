# -*- coding: utf-8 -*-
"""
ZeroDTE Replay — 0DTE options day-trading flight simulator (free demo)

Blind-pick a synthetic trading session, replay it at 0.5x-10x, trade 0DTE options
against model-priced quotes, and grade every session. Built for intraday options
traders who want deliberate practice without risking capital.

Demo data: fully synthetic sessions (statistically modeled on anonymized market
volatility profiles — NOT real market data). See README for details.

Run:  python app.py
"""
import os, sys, json, time, collections
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
import numpy as np
import pandas as pd
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import demo_feed as FEED
from trade_panel import TradePanel

APP_NAME = "ZeroDTE Replay"
VERSION = "0.1.0"
NY = "America/New_York"
CFG_PATH = os.path.join(HERE, "demo_config.json")

pg.setConfigOptions(antialias=False, useOpenGL=False, background="w", foreground="k")


def cfg_load():
    try:
        with open(CFG_PATH, encoding="utf-8") as f: return json.load(f)
    except Exception: return {}


def cfg_save(kv):
    try:
        c = cfg_load(); c.update(kv)
        with open(CFG_PATH, "w", encoding="utf-8") as f: json.dump(c, f, indent=1)
    except Exception: pass


class TimeAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            try: out.append(pd.Timestamp(v, unit="s").strftime("%H:%M" if spacing >= 60 else "%H:%M:%S"))
            except Exception: out.append("")
        return out


class Candles(pg.GraphicsObject):
    def __init__(self):
        super().__init__(); self.pic = QtGui.QPicture()

    def setData(self, x, o, h, l, c):
        self.pic = QtGui.QPicture(); qp = QtGui.QPainter(self.pic)
        gb = pg.mkBrush("#1a9850"); rb = pg.mkBrush("#d73027")
        gp = pg.mkPen("#1a9850"); rp = pg.mkPen("#d73027")
        gp.setCosmetic(True); rp.setCosmetic(True)
        w = float(np.median(np.diff(x))) * 0.8 if len(x) > 1 else 0.8
        for i in range(len(x)):
            up = c[i] >= o[i]
            qp.setPen(gp if up else rp)
            qp.drawLine(QtCore.QPointF(x[i], l[i]), QtCore.QPointF(x[i], h[i]))
            top = max(o[i], c[i]); bot = min(o[i], c[i])
            qp.setPen(QtCore.Qt.NoPen); qp.setBrush(gb if up else rb)
            qp.drawRect(QtCore.QRectF(x[i] - w / 2, bot, w, max(top - bot, 1e-9)))
        qp.end(); self.prepareGeometryChange(); self.update(); self.informViewBoundsChanged()

    def paint(self, p, *a): p.drawPicture(0, 0, self.pic)

    def boundingRect(self): return QtCore.QRectF(self.pic.boundingRect())


def stride_agg(k, sx, so, sh, sl, sc, sv):
    e = (len(sx) // k) * k; rem = len(sx) > e
    rs = lambda v: v[:e].reshape(-1, k)
    nx = rs(sx)[:, 0]; no = rs(so)[:, 0]; nc = rs(sc)[:, -1]
    nh = rs(sh).max(1) if e else np.empty(0); nl = rs(sl).min(1) if e else np.empty(0)
    nv = rs(sv).sum(1) if e else np.empty(0)
    if rem:
        nx = np.append(nx, sx[e]); no = np.append(no, so[e]); nc = np.append(nc, sc[-1])
        nh = np.append(nh, sh[e:].max()); nl = np.append(nl, sl[e:].min()); nv = np.append(nv, sv[e:].sum())
    return nx, no, nh, nl, nc, nv


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{VERSION}")
        self.resize(1500, 900)
        # ---- data state ----
        self.day = None                # current day meta dict
        self.ep = None                 # epochs (naive-wall seconds), full day
        self.arr = None                # N x 5 OHLCV
        self.vwap_all = None
        # ---- replay state ----
        self.sim_on = False
        self.paused = True
        self.hold = 0                  # dialog pause counter
        self.T = None                  # tz-aware NY timestamp
        self.idx = 0
        self.speed = 1.0
        self.last_wall = 0.0
        self.endT = None
        self._render_sig = None
        # ---- marks ----
        self.mark_e = []; self.mark_x = []; self.mark_dyn = []
        self.open_anchor = {"C": None, "P": None}
        self.pair_pts = {"C": [], "P": []}
        self.panel = None
        self._build_ui()
        self.timer = QtCore.QTimer(self); self.timer.setInterval(250)
        self.timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QtWidgets.QWidget(); self.setCentralWidget(central)
        v = QtWidgets.QVBoxLayout(central); v.setContentsMargins(6, 6, 6, 6); v.setSpacing(4)
        bar = QtWidgets.QHBoxLayout(); v.addLayout(bar)
        self.b_pick = QtWidgets.QPushButton("🎲 New Session")
        self.b_pick.setStyleSheet("font-weight:bold;color:#0a6b1c;padding:3px 12px;")
        self.b_pick.setToolTip("Blind-pick a practice session. Each day appears once per round.")
        self.b_start = QtWidgets.QPushButton("▶ Start"); self.b_start.setEnabled(False)
        self.b_end = QtWidgets.QPushButton("⏹ End"); self.b_end.setEnabled(False)
        self.cb_speed = QtWidgets.QComboBox(); self.cb_speed.addItems(["0.5x", "1x", "2x", "3x", "5x", "10x"])
        self.cb_speed.setCurrentText("1x")
        self.te_time = QtWidgets.QTimeEdit(); self.te_time.setDisplayFormat("HH:mm:ss")
        self.te_time.setTime(QtCore.QTime(9, 30, 0)); self.te_time.setMaximumWidth(90)
        self.b_jump = QtWidgets.QPushButton("⏩ Jump"); self.b_jump.setEnabled(False)
        self.cb_follow = QtWidgets.QCheckBox("Follow"); self.cb_follow.setChecked(True)
        self.cb_follow.setToolTip("Auto-scroll with the replay clock. Any manual pan/zoom turns this off.")
        self.cb_vwap = QtWidgets.QCheckBox("VWAP"); self.cb_vwap.setChecked(bool(cfg_load().get("vwap", True)))
        self.b_panel = QtWidgets.QPushButton("🎯 Order Panel")
        self.lb_clock = QtWidgets.QLabel("—")
        self.lb_clock.setStyleSheet("color:#b8006b;font-size:12pt;font-weight:bold;font-family:Consolas;")
        self.lb_info = QtWidgets.QLabel("Click 🎲 New Session to begin")
        for w in [self.b_pick, self.b_start, self.b_end, QtWidgets.QLabel("Speed"), self.cb_speed,
                  QtWidgets.QLabel("At"), self.te_time, self.b_jump, self.cb_follow, self.cb_vwap,
                  self.b_panel, QtWidgets.QLabel(" ⏱"), self.lb_clock, self.lb_info]:
            bar.addWidget(w)
        bar.addStretch(1)
        win = pg.GraphicsLayoutWidget(); v.addWidget(win, 1)
        self.pP = win.addPlot(row=0, col=0, axisItems={"bottom": TimeAxis("bottom")})
        self.pP.showGrid(x=True, y=True, alpha=0.25); self.pP.setLabel("left", "Price $")
        win.nextRow()
        self.pV = win.addPlot(row=1, col=0, axisItems={"bottom": TimeAxis("bottom")})
        self.pV.showGrid(x=True, y=True, alpha=0.25); self.pV.setLabel("left", "Volume/s")
        win.ci.layout.setRowStretchFactor(0, 4); win.ci.layout.setRowStretchFactor(1, 1)
        self.pV.setXLink(self.pP)
        self.pP.setMouseEnabled(x=True, y=False)
        for p in (self.pP, self.pV):
            p.vb.state["wheelScaleFactor"] = -1.0 / 16.0    # smooth wheel steps
        self.candle = Candles(); self.pP.addItem(self.candle)
        self.ema = self.pP.plot([], [], pen=pg.mkPen("#1f4e9c", width=1))
        self.vwapc = self.pP.plot([], [], pen=pg.mkPen("#f39c12", width=1.6))
        self.pline = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#888", style=QtCore.Qt.DashLine))
        self.pP.addItem(self.pline)
        self.vU = pg.BarGraphItem(x=[], height=[], width=0.8, brush="#1a9850", pen=None); self.pV.addItem(self.vU)
        self.vD = pg.BarGraphItem(x=[], height=[], width=0.8, brush="#d73027", pen=None); self.pV.addItem(self.vD)
        self.arrE = pg.ScatterPlotItem(pxMode=True); self.pP.addItem(self.arrE)
        self.arrX = pg.ScatterPlotItem(pxMode=True); self.pP.addItem(self.arrX)
        self.pair_item = {"C": pg.PlotDataItem([], [], connect="pairs", pen=pg.mkPen("#0a6b1c", width=1.6, style=QtCore.Qt.DashLine)),
                          "P": pg.PlotDataItem([], [], connect="pairs", pen=pg.mkPen("#a3000f", width=1.6, style=QtCore.Qt.DashLine))}
        self.live_item = {"C": pg.PlotDataItem([], [], pen=pg.mkPen("#0a6b1c", width=1.3)),
                          "P": pg.PlotDataItem([], [], pen=pg.mkPen("#a3000f", width=1.3))}
        for it in list(self.pair_item.values()) + list(self.live_item.values()):
            self.pP.addItem(it)
        # signals
        self.b_pick.clicked.connect(self.pick_session)
        self.b_start.clicked.connect(self.toggle_start)
        self.b_end.clicked.connect(self.end_session)
        self.b_jump.clicked.connect(self.jump_to)
        self.b_panel.clicked.connect(self.show_panel)
        self.cb_speed.currentTextChanged.connect(lambda s: setattr(self, "speed", float(s[:-1])))
        self.cb_vwap.stateChanged.connect(lambda *a: (cfg_save({"vwap": self.cb_vwap.isChecked()}),
                                                      self._invalidate(), self.render()))
        self.pP.vb.sigRangeChangedManually.connect(self._manual_range)
        self.pP.vb.sigRangeChanged.connect(lambda *a: self.render())
        self.pP.scene().sigMouseClicked.connect(self._on_dblclick)

    # ------------------------------------------------------------- helpers
    def _invalidate(self): self._render_sig = None

    def simx(self, T): return T.tz_localize(None).value / 1e9

    def get_panel(self):
        if self.panel is None:
            self.panel = TradePanel(HERE, mark_cb=self.add_mark, hold_cb=self._dlg_hold)
        return self.panel

    def _dlg_hold(self, flag):
        self.hold = max(0, self.hold + (1 if flag else -1))

    def show_panel(self):
        p = self.get_panel(); p.show(); p.raise_(); p.activateWindow()

    # ------------------------------------------------------------ session
    def pick_session(self):
        if self.sim_on: return
        days = FEED.list_days()
        played = dict(cfg_load().get("rounds", {}))
        minc = min(int(played.get(d["id"], 0)) for d in days)
        pool = [d for d in days if int(played.get(d["id"], 0)) <= minc]
        rnd = np.random.default_rng()
        pick = pool[int(rnd.integers(len(pool)))]
        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle("New Practice Session")
        dv = QtWidgets.QVBoxLayout(dlg)
        dv.addWidget(QtWidgets.QLabel(
            f"Round {minc + 1} — {len(pool)} of {len(days)} sessions left this round.\n"
            f"A session was blind-picked for you (its character stays hidden until you trade it).\n"
            f"Pick OK to arm the replay, then press ▶ Start when ready."))
        row = QtWidgets.QHBoxLayout(); dv.addLayout(row)
        cb = QtWidgets.QComboBox(); cb.addItems([d["id"] for d in days]); cb.setCurrentText(pick["id"])
        rb = QtWidgets.QPushButton("🎲 Reroll")
        rb.clicked.connect(lambda: cb.setCurrentText(pool[int(rnd.integers(len(pool)))]["id"]))
        te = QtWidgets.QTimeEdit(); te.setDisplayFormat("HH:mm:ss"); te.setTime(self.te_time.time())
        for w in [QtWidgets.QLabel("Session"), cb, rb, QtWidgets.QLabel("  Start at"), te]:
            row.addWidget(w)
        row.addStretch(1)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject); dv.addWidget(bb)
        if dlg.exec_() != QtWidgets.QDialog.Accepted: return
        did = cb.currentText()
        self.te_time.setTime(te.time())
        played[did] = int(played.get(did, 0)) + 1
        cfg_save({"rounds": played})
        self._load_day(did)
        self._arm()

    def _load_day(self, did):
        days = {d["id"]: d for d in FEED.list_days()}
        self.day = days[did]
        und = FEED.load_underlying(did)
        self.ep = (und.index.tz_localize(None).asi8 / 1e9).astype(float)
        self.arr = und[["open", "high", "low", "close", "volume"]].to_numpy(float)
        tp = (self.arr[:, 1] + self.arr[:, 2] + self.arr[:, 3]) / 3.0
        pv = np.cumsum(tp * self.arr[:, 4]); vv = np.cumsum(self.arr[:, 4])
        self.vwap_all = np.where(vv > 0, pv / np.maximum(vv, 1e-9), tp)
        self._invalidate()

    def _arm(self):
        qt = self.te_time.time()
        t0 = pd.Timestamp(f"{self.day['date']} {qt.toString('HH:mm:ss')}").tz_localize(NY)
        first = pd.Timestamp(self.day["date"] + " 09:30:00").tz_localize(NY)
        last = pd.Timestamp(self.day["date"] + " 15:59:59").tz_localize(NY)
        self.T = min(max(t0, first + pd.Timedelta(seconds=2)), last)
        self.endT = last + pd.Timedelta(seconds=1)
        self.sim_on = True; self.paused = True; self.hold = 0
        self.speed = float(self.cb_speed.currentText()[:-1])
        self.last_wall = time.monotonic()
        self.clear_marks()
        self.cb_follow.setChecked(True)
        self.b_pick.setEnabled(False); self.b_start.setEnabled(True)
        self.b_end.setEnabled(True); self.b_jump.setEnabled(True)
        self.b_start.setText("▶ Start")
        p = self.get_panel()
        p.set_context(self.day["id"], self.day["date"]); p.show(); p.raise_()
        tn = self.simx(self.T); W = 960.0
        self.pP.setXRange(tn - W * 0.12, tn + W * 0.88, padding=0)
        self.setWindowTitle(f"{APP_NAME} — {self.day['id']} REPLAY (armed)")
        self.lb_info.setText(f"Armed at {self.T.strftime('%H:%M:%S')} — press ▶ Start")
        self._apply(force=True)
        self.timer.start()

    def toggle_start(self):
        if not self.sim_on: return
        self.paused = not self.paused
        self.b_start.setText("▶ Resume" if self.paused else "⏸ Pause")
        if not self.paused:
            self.lb_info.setText("Replaying — fills at next-second model mid")
            self.setWindowTitle(f"{APP_NAME} — {self.day['id']} REPLAY")

    def end_session(self):
        if not self.sim_on: return
        self.timer.stop(); self.sim_on = False
        try:
            if self.panel is not None: self.panel.session_end(self.T)
        except Exception:
            import traceback; traceback.print_exc()
        finally:
            self.b_pick.setEnabled(True); self.b_start.setEnabled(False)
            self.b_start.setText("▶ Start"); self.b_end.setEnabled(False); self.b_jump.setEnabled(False)
        self.idx = len(self.ep)                       # reveal the whole day for review
        self._invalidate()
        self.pP.setXRange(float(self.ep[0]), float(self.ep[-1]), padding=0.01)
        self.render()
        self.lb_info.setText("Session ended — full day revealed for review. 🎲 for the next one.")
        self.setWindowTitle(f"{APP_NAME} — {self.day['id']} (review)")

    def jump_to(self):
        if not self.sim_on: return
        qt = self.te_time.time()
        tgt = pd.Timestamp(f"{self.day['date']} {qt.toString('HH:mm:ss')}").tz_localize(NY)
        first = pd.Timestamp(self.day["date"] + " 09:30:00").tz_localize(NY)
        tgt = min(max(tgt, first + pd.Timedelta(seconds=2)), self.endT - pd.Timedelta(seconds=1))
        if tgt < self.T and self.panel is not None and self.panel.has_positions():
            self.lb_info.setText("Cannot jump back while holding a position"); return
        self.T = tgt; self._apply(force=True)

    # ------------------------------------------------------------- engine
    def _tick(self):
        now = time.monotonic(); dtw = min(now - self.last_wall, 2.0); self.last_wall = now
        if not self.sim_on or self.paused or self.hold > 0: return
        self.T = self.T + pd.Timedelta(seconds=dtw * self.speed)
        if self.T >= self.endT:
            self.T = self.endT; self._apply(force=True)
            self.paused = True; self.b_start.setEnabled(False); self.b_jump.setEnabled(False)
            try:
                if self.panel is not None: self.panel.eod(self.T)
            except Exception: pass
            self.lb_info.setText("Market close — open positions auto-flattened. ⏹ End to review.")
            return
        self._apply()

    def _apply(self, force=False):
        tn = self.simx(self.T)
        idx = int(np.searchsorted(self.ep, tn - 1.0, side="right"))   # completed bars only
        self.lb_clock.setText(self.T.strftime("%H:%M:%S"))
        if idx != self.idx or force:
            self.idx = idx
            self._invalidate()
            if idx >= 2 and self.cb_follow.isChecked():
                d0, d1 = self.pP.vb.viewRange()[0]; W = max(d1 - d0, 30.0)
                if tn > d0 + W * 0.88 or tn < d0:
                    self.pP.setXRange(tn - W * 0.12, tn + W * 0.88, padding=0)
            self.render()
        spot = float(self.arr[idx - 1, 3]) if idx > 0 else None
        if self.panel is not None and spot is not None:
            self.panel.on_tick(self.T, spot, self.speed)
        for r in ("C", "P"):
            if self.open_anchor[r] is not None and spot is not None:
                x0, y0 = self.open_anchor[r]
                self.live_item[r].setData([x0, tn], [y0, spot])

    def _manual_range(self, *a):
        if self.sim_on and self.cb_follow.isChecked():
            self.cb_follow.setChecked(False)
            self.lb_info.setText("Manual view — Follow turned off (re-check to snap back)")

    def _on_dblclick(self, ev):
        if ev.double() and self.ep is not None:
            self.pP.setXRange(float(self.ep[0]), float(self.ep[max(0, self.idx - 1)]), padding=0.01)

    # ------------------------------------------------------------- render
    def render(self):
        if self.ep is None or self.idx < 2: return
        N = self.idx
        d0, d1 = self.pP.vb.viewRange()[0]
        sig = (N, round(d0, 2), round(d1, 2), self.cb_vwap.isChecked())
        if sig == self._render_sig: return
        self._render_sig = sig
        ep = self.ep[:N]; arr = self.arr[:N]
        mar = (d1 - d0) * 0.5 + 3; MAXD = 900
        i0 = max(0, int(np.searchsorted(ep, d0 - mar, "left")))
        i1 = min(N, max(2, int(np.searchsorted(ep, d1 + mar, "right"))))
        i0 = min(i0, i1 - 2)
        sx = ep[i0:i1]; sa = arr[i0:i1]
        so, sh, sl, sc, sv = sa[:, 0], sa[:, 1], sa[:, 2], sa[:, 3], sa[:, 4]
        if len(sx) > MAXD:
            k = int(np.ceil(len(sx) / MAXD))
            sx, so, sh, sl, sc, sv = stride_agg(k, sx, so, sh, sl, sc, sv)
        self.candle.setData(sx, so, sh, sl, sc)
        es = pd.Series(sc).ewm(span=30).mean().values
        self.ema.setData(sx, es)
        if self.cb_vwap.isChecked():
            vw = self.vwap_all[np.clip(np.searchsorted(self.ep, sx, "left"), 0, len(self.vwap_all) - 1)]
            self.vwapc.setData(sx, vw)
        else:
            self.vwapc.setData([], [])
        self.pline.setValue(float(arr[-1, 3]))
        up = sc >= so
        w = float(np.median(np.diff(sx))) * 0.8 if len(sx) > 1 else 0.8
        self.vU.setOpts(x=sx[up], height=sv[up], width=w)
        self.vD.setOpts(x=sx[~up], height=sv[~up], width=w)
        if len(sv):
            self.pV.setYRange(0, max(float(np.percentile(sv, 99)), 1.0) * 1.08, padding=0)
        ylo = float(sl.min()); yhi = float(sh.max()); ysp = max(yhi - ylo, 1e-6)
        self.pP.vb.setYRange(ylo - 0.04 * ysp, yhi + 0.04 * ysp, padding=0)

    # -------------------------------------------------------------- marks
    def clear_marks(self):
        self.mark_e = []; self.mark_x = []
        self.open_anchor = {"C": None, "P": None}; self.pair_pts = {"C": [], "P": []}
        self.arrE.setData([]); self.arrX.setData([])
        for r in ("C", "P"):
            self.pair_item[r].setData([], []); self.live_item[r].setData([], [])
        for it in self.mark_dyn: self.pP.removeItem(it)
        self.mark_dyn = []

    def add_mark(self, kind, T, y, right, label=""):
        x = self.simx(T); col = "#0a6b1c" if right == "C" else "#a3000f"
        try:
            ylo, yhi = self.pP.vb.viewRange()[1]; voff = 0.035 * max(yhi - ylo, 1e-6)
        except Exception:
            voff = 0.0
        if kind == "entry":
            ya = y - voff if right == "C" else y + voff
            self.mark_e.append({"pos": (x, ya), "symbol": "t1" if right == "C" else "t",
                                "size": 18, "brush": pg.mkBrush(col), "pen": pg.mkPen("k")})
            self.arrE.setData(self.mark_e)
            if self.open_anchor[right] is None: self.open_anchor[right] = (x, y)
            anchor = (0.5, -0.15) if right == "C" else (0.5, 1.15)
        else:
            self.mark_x.append({"pos": (x, y), "symbol": "star", "size": 18,
                                "brush": pg.mkBrush("#e0a800"), "pen": pg.mkPen("k")})
            self.arrX.setData(self.mark_x)
            if self.open_anchor[right] is not None:
                x0, y0 = self.open_anchor[right]
                self.pair_pts[right].extend([(x0, y0), (x, y)])
                a = np.array(self.pair_pts[right])
                self.pair_item[right].setData(a[:, 0], a[:, 1])
                self.open_anchor[right] = None
                self.live_item[right].setData([], [])
            ya = y; anchor = (0.5, 1.35); col = "#7a5c00"
        if label:
            t = pg.TextItem(label, color=col, anchor=anchor); t.setPos(x, ya)
            self.pP.addItem(t); self.mark_dyn.append(t)


def main():
    app = pg.mkQApp(APP_NAME)
    mw = MainWindow(); mw.show()
    app.exec_()


if __name__ == "__main__":
    main()
