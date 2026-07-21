# -*- coding: utf-8 -*-
"""
trade_panel.py — order panel for ZeroDTE Replay (demo)

Rules (mirrors a real one-click 0DTE panel):
  * BUY  = nearest OTM 0DTE contract (CALL: lowest strike > spot, PUT: highest strike < spot)
  * BUY while holding = add at the SAME strike (average cost updates)
  * SELL = close the whole side at once;  never sell to open
Fills = model mid at the NEXT second after your click (no look-ahead).
Every trade is journaled; ending a session prompts a grade (A/B/C) + review notes,
archived to trading_log/{session}/T{n}_{grade}_{minutes}_{speed}x.csv/.txt
"""
import os, csv, time, datetime as _dt
import numpy as np
import pandas as pd
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

import demo_feed as FEED

MULT = 100
QTY_MAX = 100
JOURNAL = "journal.csv"
JHEADER = ["session", "day", "sim_time", "action", "right", "strike",
           "qty", "fill", "spot", "realized_usd", "hold_secs", "note"]

BG = "#1e1e1e"; FG = "#e0e0e0"; GRAY = "#9e9e9e"
C_COL = "#66bb6a"; P_COL = "#ef5350"


class DurAxis(pg.AxisItem):
    """x = seconds since entry (always starts at 0)."""
    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            try:
                s = int(round(v)); sign = "-" if s < 0 else ""; s = abs(s)
                out.append(f"{sign}{s//3600}:{s%3600//60:02d}:{s%60:02d}" if s >= 3600
                           else f"{sign}{s//60}:{s%60:02d}")
            except Exception:
                out.append("")
        return out


class TradePanel(QtWidgets.QWidget):
    def __init__(self, appdir, mark_cb=None, hold_cb=None):
        super().__init__()
        self.appdir = appdir
        self.mark_cb = mark_cb
        self.hold_cb = hold_cb
        self.logdir = os.path.join(appdir, "trading_log")
        os.makedirs(self.logdir, exist_ok=True)
        self.jpath = os.path.join(self.logdir, JOURNAL)
        self.day_id = None; self.date = None; self.session = None
        self.qs = None                      # DemoQuoteSource
        self.strikes = []
        self.T = None; self.spot = None; self.running = False
        self.held = {"C": None, "P": None}
        self.curve_x = {"C": [], "P": []}; self.curve_y = {"C": [], "P": []}
        self._last_cx = {"C": None, "P": None}
        self.realized = 0.0; self.nclose = 0; self.nwin = 0; self.nbuy = 0
        self.sess_rows = []; self.sess_t0 = None; self.last_speed = 1.0
        self._sess_done = True
        self._jbuf = []; self._jwarn = False; self._jnext = 0.0
        self._tick_err = False
        self._build()

    # ------------------------------------------------------------------ UI
    def _build(self):
        self.setWindowTitle("Order Panel — ZeroDTE Replay")
        self.setStyleSheet(f"QWidget{{background:{BG};color:{FG};font-family:'Segoe UI';}}"
                           f"QCheckBox{{color:{FG};}} QSpinBox{{background:#2b2b2b;color:{FG};}}")
        v = QtWidgets.QVBoxLayout(self); v.setContentsMargins(8, 8, 8, 8); v.setSpacing(5)
        self.banner = QtWidgets.QLabel("No session — click 🎲 New Session in the chart window")
        self.banner.setStyleSheet("background:#444;color:#fff;font-size:12pt;font-weight:bold;padding:5px;")
        self.banner.setAlignment(QtCore.Qt.AlignCenter)
        v.addWidget(self.banner)
        g = QtWidgets.QGridLayout(); g.setHorizontalSpacing(10); v.addLayout(g)

        def big(text, color, cb):
            b = QtWidgets.QPushButton(text)
            b.setStyleSheet(f"QPushButton{{background:{color};color:white;font-size:13pt;font-weight:bold;"
                            f"padding:16px 10px;border:2px solid #555;border-radius:4px;}}"
                            f"QPushButton:disabled{{background:#3a3a3a;color:#777;}}")
            b.clicked.connect(cb); b.setMinimumWidth(160)
            return b

        self.tgt = {"C": QtWidgets.QLabel("BUY CALL → —"), "P": QtWidgets.QLabel("BUY PUT → —")}
        self.tgt["C"].setStyleSheet(f"color:{C_COL};font-family:Consolas;font-size:11pt;font-weight:bold;")
        self.tgt["P"].setStyleSheet(f"color:{P_COL};font-family:Consolas;font-size:11pt;font-weight:bold;")
        g.addWidget(self.tgt["C"], 0, 0, QtCore.Qt.AlignCenter)
        g.addWidget(self.tgt["P"], 0, 2, QtCore.Qt.AlignCenter)

        center = QtWidgets.QVBoxLayout()
        self.lb_clock = QtWidgets.QLabel("⏱ —")
        self.lb_clock.setStyleSheet("color:#f59e0b;font-family:Consolas;font-size:13pt;font-weight:bold;")
        self.lb_spot = QtWidgets.QLabel("—\n—")
        self.lb_spot.setStyleSheet("color:#fff;font-family:Consolas;font-size:14pt;font-weight:bold;")
        self.lb_hold = QtWidgets.QLabel("Held ⏱ —")
        self.lb_hold.setStyleSheet("color:#f59e0b;font-family:Consolas;font-size:12pt;font-weight:bold;")
        self.lb_pnl = QtWidgets.QLabel("P&L  —")
        self.lb_pnl.setStyleSheet(f"color:{GRAY};font-family:Consolas;font-size:13pt;font-weight:bold;")
        self.lb_qty = QtWidgets.QLabel("Position 0")
        self.lb_qty.setStyleSheet("color:#bbb;font-family:Consolas;font-size:11pt;font-weight:bold;")
        self.lb_sum = QtWidgets.QLabel("Realized $0 · 0 closed · win —")
        self.lb_sum.setStyleSheet("color:#9cf;font-family:Consolas;font-size:10pt;")
        for w in (self.lb_clock, self.lb_spot, self.lb_hold, self.lb_pnl, self.lb_qty, self.lb_sum):
            w.setAlignment(QtCore.Qt.AlignCenter); center.addWidget(w)
        cw = QtWidgets.QWidget(); cw.setLayout(center)
        g.addWidget(cw, 1, 1, 3, 1)

        self.b_buy = {"C": big("BUY CALL", "#2e7d32", lambda: self._buy("C")),
                      "P": big("BUY PUT", "#2e7d32", lambda: self._buy("P"))}
        g.addWidget(self.b_buy["C"], 1, 0); g.addWidget(self.b_buy["P"], 1, 2)
        self.ind = {}
        for col, r in ((0, "C"), (2, "P")):
            f = QtWidgets.QHBoxLayout(); f.addStretch(1)
            f.addWidget(QtWidgets.QLabel("pos"))
            self.ind[r] = QtWidgets.QLabel(); self.ind[r].setFixedSize(24, 24)
            f.addWidget(self.ind[r]); f.addStretch(1)
            fw = QtWidgets.QWidget(); fw.setLayout(f); fw.setStyleSheet("background:#d9d9d9;color:#222;")
            g.addWidget(fw, 2, col)
            self._sq(r, "off")
        self.b_sell = {"C": big("CLOSE CALL", "#c62828", lambda: self._sell("C")),
                       "P": big("CLOSE PUT", "#c62828", lambda: self._sell("P"))}
        g.addWidget(self.b_sell["C"], 3, 0); g.addWidget(self.b_sell["P"], 3, 2)
        self.pos_lbl = {}
        for col, r in ((0, "C"), (2, "P")):
            self.pos_lbl[r] = QtWidgets.QLabel("—")
            self.pos_lbl[r].setStyleSheet("color:#bbb;font-family:Consolas;font-size:9pt;")
            self.pos_lbl[r].setAlignment(QtCore.Qt.AlignCenter)
            g.addWidget(self.pos_lbl[r], 4, col)

        hb = QtWidgets.QHBoxLayout(); v.addLayout(hb)
        hb.addWidget(QtWidgets.QLabel("Qty:"))
        self.sp_qty = QtWidgets.QSpinBox(); self.sp_qty.setRange(1, QTY_MAX); self.sp_qty.setValue(1)
        self.sp_qty.setMaximumWidth(70); hb.addWidget(self.sp_qty)
        self.cb_confirm = QtWidgets.QCheckBox("Confirm before order (pauses clock)")
        hb.addWidget(self.cb_confirm)
        self.cb_top = QtWidgets.QCheckBox("Always on top"); self.cb_top.stateChanged.connect(self._top)
        hb.addWidget(self.cb_top); hb.addStretch(1)
        jb = QtWidgets.QPushButton("📒 Open journal CSV")
        jb.setStyleSheet("padding:3px 8px;background:#1f4d7a;color:white;font-weight:bold;")
        jb.clicked.connect(self._open_journal); hb.addWidget(jb)

        self.plot = pg.PlotWidget(axisItems={"bottom": DurAxis(orientation="bottom")})
        self.plot.setBackground("w"); self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setMinimumHeight(190)
        self.plot.setLabel("left", "Option $"); self.plot.setLabel("bottom", "Time held")
        self.plot.setTitle("Held option price — starts at 0:00 on entry (green=CALL red=PUT, dash=cost)", size="9pt")
        self.curve = {"C": self.plot.plot([], [], pen=pg.mkPen("#1a9850", width=1.6), connect="finite"),
                      "P": self.plot.plot([], [], pen=pg.mkPen("#d73027", width=1.6), connect="finite")}
        self.avg_line = {"C": pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#1a9850", style=QtCore.Qt.DashLine)),
                         "P": pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#d73027", style=QtCore.Qt.DashLine))}
        for ln in self.avg_line.values():
            self.plot.addItem(ln); ln.setVisible(False)
        v.addWidget(self.plot, 1)

        self.logbox = QtWidgets.QTextEdit(); self.logbox.setReadOnly(True)
        self.logbox.setStyleSheet("background:#0f0f0f;color:#cfcfcf;font-family:Consolas;font-size:9pt;")
        self.logbox.setMinimumHeight(110); self.logbox.setMaximumHeight(160)
        v.addWidget(self.logbox)
        self._enable(False)
        self.resize(600, 860)

    def _top(self):
        try:
            self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, self.cb_top.isChecked()); self.show()
        except Exception: pass

    def _sq(self, r, mode):
        col = {"green": "#16a34a", "red": "#dc2626"}.get(mode)
        self.ind[r].setStyleSheet(f"background:{col};border:2px solid #333;" if col
                                  else "background:#d9d9d9;border:2px solid #000;")

    def _log(self, msg):
        ts = self.T.tz_localize(None).strftime("%H:%M:%S") if self.T is not None else "--:--:--"
        self.logbox.append(f"[{ts}] {msg}")
        sb = self.logbox.verticalScrollBar(); sb.setValue(sb.maximum())

    def _enable(self, on):
        for r in ("C", "P"):
            self.b_buy[r].setEnabled(bool(on and self.strikes))
            self.b_sell[r].setEnabled(bool(on and self.held[r]))

    def _clear_curve(self, r):
        self.curve_x[r] = []; self.curve_y[r] = []; self._last_cx[r] = None
        self.curve[r].setData([], []); self.avg_line[r].setVisible(False)

    # ------------------------------------------------------------ session
    def set_context(self, day_id, date):
        self.day_id = day_id; self.date = date
        self.session = f"{day_id}_{_dt.datetime.now():%Y%m%d%H%M%S}"
        self.qs = FEED.DemoQuoteSource(day_id)
        self.strikes = FEED.strikes(day_id)
        self.T = None; self.spot = None; self.running = True
        self.held = {"C": None, "P": None}
        for r in ("C", "P"):
            self._clear_curve(r); self._sq(r, "off"); self.pos_lbl[r].setText("—")
        self.lb_hold.setText("Held ⏱ —"); self.lb_pnl.setText("P&L  —")
        self.lb_pnl.setStyleSheet(f"color:{GRAY};font-family:Consolas;font-size:13pt;font-weight:bold;")
        self.lb_qty.setText("Position 0")
        self.realized = 0.0; self.nclose = 0; self.nwin = 0; self.nbuy = 0
        self.sess_rows = []; self.sess_t0 = None; self._sess_done = False
        self._tick_err = False
        try: self.plot.plotItem.vb.enableAutoRange()
        except Exception: pass
        self._summary()
        self.banner.setText(f"🎬 LIVE — {day_id} (synthetic session)")
        self.banner.setStyleSheet("background:#0a5c2b;color:#fff;font-size:12pt;font-weight:bold;padding:5px;")
        self._log(f"── session {self.session} · {len(self.strikes)} strikes · fills = next-second model mid ──")
        self._enable(True)

    def has_positions(self):
        return bool(self.held["C"] or self.held["P"])

    def eod(self, T):
        self.T = T
        self._flatten("auto-flatten at close")
        self.running = False; self._enable(False)
        self._jflush(final=True)
        self.banner.setText(f"🏁 Close — {self.day_id} · realized ${self.realized:+,.0f}")
        self.banner.setStyleSheet("background:#7a5c00;color:#fff;font-size:12pt;font-weight:bold;padding:5px;")
        self._session_prompt()

    def session_end(self, T):
        if self.T is None and T is not None: self.T = T
        if self.has_positions(): self._flatten("flatten on end")
        self.running = False; self._enable(False)
        self._jflush(final=True)
        if self.qs is not None: self.qs.shutdown()
        self.banner.setText(f"⏹ Ended — {self.day_id} · realized ${self.realized:+,.0f}")
        self.banner.setStyleSheet("background:#444;color:#fff;font-size:12pt;font-weight:bold;padding:5px;")
        self._session_prompt()

    # ------------------------------------------------------------------ tick
    def on_tick(self, T, spot, speed=None):
        try:
            if speed: self.last_speed = float(speed)
            if self.sess_t0 is None and self.running: self.sess_t0 = T
            self._tick_impl(T, spot)
        except Exception as e:
            if not self._tick_err:
                self._tick_err = True
                import traceback; traceback.print_exc()
                self._log(f"⚠ panel refresh error (reported once): {type(e).__name__}: {e}")

    def _tick_impl(self, T, spot):
        if not self.running: return
        self.T = T; self.spot = float(spot)
        # ★报价时间基=图表同款"已完成的上一秒" — 期权与股价零错位(否则期权领先1-2秒, 0.5x下肉眼可见);
        #   成交仍用点击后的下一秒(F1), 无前视
        self.Tq = T.floor("1s") - pd.Timedelta(seconds=1)
        self.lb_clock.setText(f"⏱ {T.tz_localize(None):%H:%M:%S}")
        self.lb_spot.setText(f"{self.day_id}\n{self.spot:.2f}")
        pnl_tot = 0.0; pnl_any = False; qty_tot = 0; parts = []; entries = []
        for r in ("C", "P"):
            held = self.held[r]; nm = "CALL" if r == "C" else "PUT"
            if held:
                px = self.qs.now(r, held["strike"], self.Tq)
                self.tgt[r].setText(f"ADD {nm} {held['strike']:g} @ {px:.2f}" if px == px
                                    else f"ADD {nm} {held['strike']:g} @ —")
                if px == px:
                    pnl_tot += (px - held["avg"]) * MULT * held["qty"]; pnl_any = True
                qty_tot += held["qty"]; parts.append(f"{r}:{held['qty']}")
                entries.append(held["entryT"])
                self.pos_lbl[r].setText(f"{held['qty']}x {held['strike']:g}{r} avg {held['avg']:.2f}")
                if px == px:
                    xel = (T - held["entryT"]).total_seconds()
                    sec = int(xel)
                    if self.curve_x[r] and xel < self.curve_x[r][-1]:
                        self._clear_curve(r)
                        self.avg_line[r].setValue(held["avg"]); self.avg_line[r].setVisible(True)
                    if self._last_cx[r] != sec:
                        self._last_cx[r] = sec
                        if self.curve_x[r] and xel - self.curve_x[r][-1] > 60:
                            self.curve_x[r].append(xel); self.curve_y[r].append(np.nan)
                        self.curve_x[r].append(xel); self.curve_y[r].append(px)
                        self.curve[r].setData(np.asarray(self.curve_x[r]), np.asarray(self.curve_y[r]))
            else:
                k, _ = self._pick_otm(r)
                px = self.qs.now(r, k, self.Tq) if k is not None else np.nan
                self.tgt[r].setText(f"BUY {nm} → {k:g} @ {px:.2f}" if (k is not None and px == px)
                                    else f"BUY {nm} → —")
        if entries:
            hs = (T - min(entries)).total_seconds()
            self.lb_hold.setText(f"Held ⏱ {int(hs//60):02d}:{int(hs%60):02d}")
        else:
            self.lb_hold.setText("Held ⏱ —")
        if pnl_any:
            col = "#4ade80" if pnl_tot >= 0 else "#f87171"
            self.lb_pnl.setText(f"P&L {pnl_tot:+,.0f}$")
            self.lb_pnl.setStyleSheet(f"color:{col};font-family:Consolas;font-size:13pt;font-weight:bold;")
        else:
            self.lb_pnl.setText("P&L  —")
            self.lb_pnl.setStyleSheet(f"color:{GRAY};font-family:Consolas;font-size:13pt;font-weight:bold;")
        self.lb_qty.setText(f"Position {qty_tot}" + (f" ({' '.join(parts)})" if len(parts) > 1 else ""))
        if self._jbuf and time.monotonic() >= self._jnext:
            self._jnext = time.monotonic() + 5.0
            self._jflush()

    # ------------------------------------------------------------- trading
    def _pick_otm(self, right):
        if self.spot is None or not self.strikes: return None, None
        if right == "C":
            c = [k for k in self.strikes if k > self.spot]
            return (min(c), None) if c else (None, None)
        c = [k for k in self.strikes if k < self.spot]
        return (max(c), None) if c else (None, None)

    def _ask(self, title, text, yes="Confirm", no="Cancel"):
        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle(title)
        dlg.setWindowFlags(dlg.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)
        v = QtWidgets.QVBoxLayout(dlg); v.addWidget(QtWidgets.QLabel(text))
        bb = QtWidgets.QDialogButtonBox()
        bb.addButton(yes, QtWidgets.QDialogButtonBox.AcceptRole)
        bb.addButton(no, QtWidgets.QDialogButtonBox.RejectRole)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject); v.addWidget(bb)
        dlg.show(); dlg.raise_(); dlg.activateWindow()
        return dlg.exec_() == QtWidgets.QDialog.Accepted

    def _confirm(self, text):
        if self.hold_cb: self.hold_cb(True)
        try: return self._ask("Confirm order", text)
        finally:
            if self.hold_cb: self.hold_cb(False)

    def _buy(self, right):
        if not self.running or self.T is None or self.spot is None: return
        q = int(self.sp_qty.value())
        held = self.held[right]
        strike = held["strike"] if held else self._pick_otm(right)[0]
        if strike is None:
            self._log("✗ no OTM strike available"); return
        if self.cb_confirm.isChecked():
            quote = self.qs.now(right, strike, getattr(self, "Tq", self.T))
            qt = f"{quote:.2f}" if quote == quote else "—"
            if not self._confirm(f"Buy {q}x {strike:g}{right} @ ~{qt} (fills next second)?"):
                return
        px, tf = self.qs.f1(right, strike, self.T)
        if not (px == px and px > 0):
            self._log(f"✗ no fill available for {strike:g}{right}"); return
        if held:
            tot = held["qty"] + q
            held["avg"] = (held["avg"] * held["qty"] + px * q) / tot; held["qty"] = tot
            note = "add (same strike)"
        else:
            self.held[right] = {"strike": strike, "qty": q, "avg": px, "entryT": self.T}
            note = "open"
            self._clear_curve(right)
            self.curve_x[right] = [0.0]; self.curve_y[right] = [px]; self._last_cx[right] = 0
            self.curve[right].setData(np.asarray(self.curve_x[right]), np.asarray(self.curve_y[right]))
            try: self.plot.plotItem.vb.enableAutoRange()
            except Exception: pass
        self.avg_line[right].setValue(self.held[right]["avg"]); self.avg_line[right].setVisible(True)
        self.nbuy += 1
        self._sq(right, "green"); self.b_sell[right].setEnabled(True)
        self._journal("BUY", right, strike, q, px, "", "", note)
        self._log(f"✔ BUY {q}x {strike:g}{right} @ {px:.2f} (spot {self.spot:.2f}) {note}")
        if self.mark_cb:
            self.mark_cb("entry", self.T, self.spot, right, f"{right} buy {px:.2f}")

    def _sell(self, right):
        if self.T is None: return
        held = self.held[right]
        if not held:
            self._log("✗ nothing to close"); return
        if self.cb_confirm.isChecked():
            quote = self.qs.now(right, held["strike"], getattr(self, "Tq", self.T))
            qt = f"{quote:.2f}" if quote == quote else "—"
            if not self._confirm(f"Close {held['qty']}x {held['strike']:g}{right} @ ~{qt} (fills next second)?"):
                return
        px, tf = self.qs.f1(right, held["strike"], self.T)
        if not (px == px):
            self._log("✗ no fill available"); return
        self._close(right, px, "manual close")

    def _close(self, right, px, note):
        held = self.held[right]
        if not held: return
        realized = (px - held["avg"]) * MULT * held["qty"]
        hold_s = int((self.T - held["entryT"]).total_seconds()) if self.T is not None else ""
        self.realized += realized; self.nclose += 1
        if realized > 0: self.nwin += 1
        self._journal("SELL", right, held["strike"], held["qty"], px, f"{realized:+.0f}", hold_s, note)
        self._log(f"★ CLOSE {held['qty']}x {held['strike']:g}{right} @ {px:.2f} → {realized:+,.0f}$ ({note})")
        if self.mark_cb and self.spot is not None:
            self.mark_cb("exit", self.T, self.spot, right, f"close {px:.2f} {realized:+,.0f}$")
        self.held[right] = None
        self._clear_curve(right)
        self._sq(right, "off"); self.b_sell[right].setEnabled(False)
        self.pos_lbl[right].setText("—")
        self._summary()

    def _flatten(self, note):
        for r in ("C", "P"):
            held = self.held[r]
            if not held: continue
            px = self.qs.now(r, held["strike"], self.T)
            if not (px == px): px = held["avg"]
            self._close(r, px, note)

    def _summary(self):
        wr = f"{self.nwin/self.nclose*100:.0f}%" if self.nclose else "—"
        self.lb_sum.setText(f"Realized ${self.realized:+,.0f} · {self.nclose} closed/{self.nbuy} buys · win {wr}")

    # ------------------------------------------------------- session archive
    def _session_prompt(self):
        if self._sess_done: return
        self._sess_done = True
        if not self.sess_rows:
            self._log("no trades this session — nothing to archive"); return
        self.show(); self.raise_(); self.activateWindow()
        if not self._ask("Save session?",
                         f"{len(self.sess_rows)} journal rows, realized ${self.realized:+,.0f}.\n"
                         f"Archive this session with a grade + review notes?",
                         yes="Save", no="Discard"):
            self._log("session not archived"); return
        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle("Grade this session")
        dlg.setWindowFlags(dlg.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)
        v = QtWidgets.QVBoxLayout(dlg)
        v.addWidget(QtWidgets.QLabel("How did you trade?"))
        hb = QtWidgets.QHBoxLayout(); v.addLayout(hb)
        rbs = {}
        for k, txt in (("A", "A — good"), ("B", "B — okay"), ("C", "C — poor")):
            rbs[k] = QtWidgets.QRadioButton(txt); hb.addWidget(rbs[k])
        rbs["B"].setChecked(True); hb.addStretch(1)
        v.addWidget(QtWidgets.QLabel("Review notes:"))
        notes = QtWidgets.QTextEdit(); notes.setMinimumHeight(140); notes.setMinimumWidth(440)
        notes.setPlaceholderText("What went right / wrong / one thing to change next session...")
        v.addWidget(notes)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject); v.addWidget(bb)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            self._log("session not archived (grade cancelled)"); return
        grade = "A" if rbs["A"].isChecked() else ("C" if rbs["C"].isChecked() else "B")
        self._save_session(grade, notes.toPlainText())

    def _save_session(self, grade, note):
        try:
            import re, glob
            d = os.path.join(self.logdir, self.day_id)
            os.makedirs(d, exist_ok=True)
            n = 1
            for f in glob.glob(os.path.join(d, "T*.csv")):
                m = re.match(r"T(\d+)_", os.path.basename(f))
                if m: n = max(n, int(m.group(1)) + 1)
            dur = 0
            if self.sess_t0 is not None and self.T is not None:
                dur = max(0, int(round((self.T - self.sess_t0).total_seconds() / 60)))
            base = f"T{n}_{grade}_{dur}_{self.last_speed:g}x"
            with open(os.path.join(d, base + ".csv"), "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f); w.writerow(JHEADER)
                for row in self.sess_rows: w.writerow(row)
            t0 = self.sess_t0.tz_localize(None).strftime("%H:%M:%S") if self.sess_t0 is not None else "—"
            t1 = self.T.tz_localize(None).strftime("%H:%M:%S") if self.T is not None else "—"
            wr = f"{self.nwin/self.nclose*100:.0f}%" if self.nclose else "—"
            with open(os.path.join(d, base + ".txt"), "w", encoding="utf-8") as f:
                f.write(f"Session: {self.day_id}   attempt #{n}   grade {grade}\n"
                        f"Window: {t0} → {t1} ({dur} min)   speed {self.last_speed:g}x\n"
                        f"Realized ${self.realized:+,.0f}   {self.nclose} closed/{self.nbuy} buys   win {wr}\n"
                        f"{'-'*44}\nNotes:\n{note}\n")
            self._log(f"💾 archived trading_log/{self.day_id}/{base}.csv (+notes)")
        except Exception as e:
            self._log(f"⚠ archive failed: {type(e).__name__}: {e}")

    # ------------------------------------------------------------- journal
    def _journal(self, action, right, strike, qty, px, realized, hold_s, note):
        row = [self.session, self.day_id,
               self.T.tz_localize(None).strftime("%H:%M:%S") if self.T is not None else "",
               action, right, f"{strike:g}", qty, f"{px:.2f}",
               f"{self.spot:.2f}" if self.spot is not None else "",
               realized, hold_s, note]
        self.sess_rows.append(list(row))
        self._jbuf.append(row)
        self._jflush()

    def _jflush(self, final=False):
        if not self._jbuf: return
        try:
            new = not os.path.exists(self.jpath)
            with open(self.jpath, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if new: w.writerow(JHEADER)
                for row in self._jbuf: w.writerow(row)
            self._jbuf = []
            if self._jwarn:
                self._jwarn = False; self._log("✔ journal backlog flushed")
        except Exception:
            if not self._jwarn:
                self._jwarn = True
                self._log("⚠ journal file locked (Excel open?) — buffering, will retry")
            if final:
                try:
                    pp = self.jpath.replace(".csv", "_pending.csv")
                    new = not os.path.exists(pp)
                    with open(pp, "a", newline="", encoding="utf-8-sig") as f:
                        w = csv.writer(f)
                        if new: w.writerow(JHEADER)
                        for row in self._jbuf: w.writerow(row)
                    self._jbuf = []
                    self._log(f"📥 wrote backlog to {os.path.basename(pp)}")
                except Exception:
                    self._log("✗ pending file also failed; rows kept in memory")

    def _open_journal(self):
        if os.path.exists(self.jpath):
            self._log("note: keeping it open in Excel locks writes (auto-buffered)")
            try: os.startfile(self.jpath)
            except Exception as e: self._log(f"open failed: {e}")
        else:
            self._log("no journal yet")
