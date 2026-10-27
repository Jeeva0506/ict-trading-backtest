"""
PO3 Real-Time Chart — Plotly candlestick with zone overlays.

Generates an interactive HTML chart showing:
  - 3-min candlesticks (current session)
  - 15-min SMA50 overlaid
  - 15-min OB zones (shaded rectangles)
  - Accumulation range (dashed box)
  - Manipulation candle (highlighted)
  - iFVG zone (shaded entry zone)
  - Entry / SL / TP level lines
  - Trade markers (entry ▲/▼, exit ×)

Usage:
    chart = PO3Chart()
    chart.update(df_3m, df_15m, state_dict)   # call after each bar
    # Chart auto-saved to live/charts/po3_chart.html
    # Open in browser — manual refresh or auto-refresh with JS

The chart is rebuilt from scratch on each update (stateless, fast).
"""
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

CHART_DIR = ROOT / "live" / "charts"
CHART_DIR.mkdir(parents=True, exist_ok=True)
CHART_FILE = CHART_DIR / "po3_chart.html"

# Colour palette
C_OB_BULL   = "rgba(0,  180, 100, 0.15)"
C_OB_BEAR   = "rgba(220, 50,  50,  0.15)"
C_OB_BORDER_BULL = "rgba(0, 180, 100, 0.6)"
C_OB_BORDER_BEAR = "rgba(220, 50, 50, 0.6)"
C_ACCUM     = "rgba(255, 200,  0,  0.12)"
C_ACCUM_BDR = "rgba(255, 200,  0,  0.7)"
C_IFVG_BULL = "rgba(0,  120, 255, 0.18)"
C_IFVG_BEAR = "rgba(255, 120,  0,  0.18)"
C_MANIP     = "rgba(180,   0, 220, 0.25)"
C_SL        = "rgba(220,  50,  50, 0.9)"
C_ENTRY     = "rgba(0,   180, 100, 0.9)"
C_TP        = "rgba(0,   100, 220, 0.9)"
C_SMA       = "rgba(255, 170,   0, 0.85)"


def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


class PO3Chart:
    """
    Build and save the PO3 chart for the current trading session.

    state_dict keys (all optional):
        accum_high, accum_low       — accumulation range
        manip_idx                   — index into df_3m of manipulation bar
        ifvg                        — dict {low, high}
        direction                   — 'bull' | 'bear'
        ob_zones                    — list of OB dicts from find_ob_zones()
        trade                       — dict {entry_price, sl_spot, tp, exit_price, exit_ts, direction}
    """

    def __init__(self, output_path: Path = CHART_FILE):
        self.output_path = output_path
        if not HAS_PLOTLY:
            raise ImportError("plotly not installed. Run: pip install plotly")

    def update(self,
               df_3m:  pd.DataFrame,
               df_15m: pd.DataFrame,
               state:  Dict,
               title:  str = "PO3 — Nifty 3-min") -> None:
        """
        Rebuild and save the chart. Call after each 3-min bar close.
        df_3m / df_15m: today's data (or last N bars).
        state: current strategy state dict.
        """
        if df_3m.empty:
            return

        fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            row_heights=[0.75, 0.25],
            vertical_spacing=0.04,
            subplot_titles=[title, "Volume"],
        )

        x = df_3m.index

        # ── Candlesticks ──────────────────────────────────────────────────────
        fig.add_trace(go.Candlestick(
            x=x,
            open=df_3m["Open"],
            high=df_3m["High"],
            low=df_3m["Low"],
            close=df_3m["Close"],
            name="Nifty 3m",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
            increasing_fillcolor="#26a69a",
            decreasing_fillcolor="#ef5350",
        ), row=1, col=1)

        # ── Volume bars ───────────────────────────────────────────────────────
        colors = ["#26a69a" if c >= o else "#ef5350"
                  for c, o in zip(df_3m["Close"], df_3m["Open"])]
        fig.add_trace(go.Bar(
            x=x,
            y=df_3m["Volume"],
            name="Volume",
            marker_color=colors,
            showlegend=False,
        ), row=2, col=1)

        # ── 15-min SMA50 mapped to 3-min timeline ─────────────────────────────
        if len(df_15m) >= 50:
            sma50_15m = _sma(df_15m["Close"], 50).dropna()
            if not sma50_15m.empty:
                # Forward-fill SMA50 to 3-min timestamps
                sma50_reindexed = sma50_15m.reindex(
                    sma50_15m.index.union(df_3m.index)
                ).ffill().reindex(df_3m.index)
                fig.add_trace(go.Scatter(
                    x=df_3m.index,
                    y=sma50_reindexed,
                    mode="lines",
                    name="SMA50 (15m)",
                    line=dict(color=C_SMA, width=1.5, dash="dot"),
                ), row=1, col=1)

        # ── OB zones ──────────────────────────────────────────────────────────
        ob_zones: List[Dict] = state.get("ob_zones", [])
        x_start = x[0]
        x_end   = x[-1]
        for z in ob_zones[:6]:   # show up to 6 most recent OBs
            fc = C_OB_BULL if z["direction"] == "bull" else C_OB_BEAR
            bc = C_OB_BORDER_BULL if z["direction"] == "bull" else C_OB_BORDER_BEAR
            fig.add_shape(type="rect",
                          x0=x_start, x1=x_end,
                          y0=z["low"],  y1=z["high"],
                          line=dict(color=bc, width=1, dash="dot"),
                          fillcolor=fc,
                          row=1, col=1)
            fig.add_annotation(
                x=x_start, y=z["high"],
                text=f"OB ({z['direction'].upper()})",
                showarrow=False,
                font=dict(size=9, color=bc),
                xanchor="left", yanchor="bottom",
                row=1, col=1,
            )

        # ── Accumulation range ────────────────────────────────────────────────
        accum_high = state.get("accum_high")
        accum_low  = state.get("accum_low")
        if accum_high and accum_low:
            fig.add_shape(type="rect",
                          x0=x_start, x1=x_end,
                          y0=accum_low, y1=accum_high,
                          line=dict(color=C_ACCUM_BDR, width=1.5, dash="dash"),
                          fillcolor=C_ACCUM,
                          row=1, col=1)
            fig.add_annotation(
                x=x_start, y=accum_high,
                text="ACCUM RANGE", showarrow=False,
                font=dict(size=9, color="#ffcc00"),
                xanchor="left", yanchor="bottom",
                row=1, col=1,
            )

        # ── Manipulation candle highlight ─────────────────────────────────────
        manip_idx = state.get("manip_idx")
        if manip_idx is not None and 0 <= manip_idx < len(df_3m):
            mx = df_3m.index[manip_idx]
            fig.add_shape(type="rect",
                          x0=mx - pd.Timedelta(minutes=1),
                          x1=mx + pd.Timedelta(minutes=2),
                          y0=df_3m["Low"].iloc[manip_idx],
                          y1=df_3m["High"].iloc[manip_idx],
                          line=dict(color="rgba(180,0,220,0.9)", width=2),
                          fillcolor=C_MANIP,
                          row=1, col=1)
            fig.add_annotation(
                x=mx, y=df_3m["High"].iloc[manip_idx],
                text="MANIP", showarrow=True,
                arrowhead=2, arrowcolor="#b400dc",
                font=dict(size=9, color="#b400dc"),
                ay=-20,
                row=1, col=1,
            )

        # ── iFVG zone ─────────────────────────────────────────────────────────
        ifvg = state.get("ifvg")
        direction = state.get("direction", "bull")
        if ifvg:
            fc = C_IFVG_BULL if direction == "bull" else C_IFVG_BEAR
            label_color = "#0078ff" if direction == "bull" else "#ff7800"
            fig.add_shape(type="rect",
                          x0=x_start, x1=x_end,
                          y0=ifvg["low"], y1=ifvg["high"],
                          line=dict(color=label_color, width=2),
                          fillcolor=fc,
                          row=1, col=1)
            fig.add_annotation(
                x=x_end, y=ifvg["mid"],
                text="iFVG ENTRY ZONE", showarrow=False,
                font=dict(size=10, color=label_color, family="Arial Black"),
                xanchor="right",
                row=1, col=1,
            )

        # ── Trade levels ──────────────────────────────────────────────────────
        trade = state.get("trade")
        if trade:
            ep = trade.get("entry_price")
            sl = trade.get("sl_spot")
            tp = trade.get("tp")
            if ep:
                fig.add_hline(y=ep, line_color=C_ENTRY, line_width=1.5,
                              line_dash="solid", row=1, col=1,
                              annotation_text=f"Entry {ep:.0f}",
                              annotation_font_color=C_ENTRY)
            if sl:
                fig.add_hline(y=sl, line_color=C_SL, line_width=1.5,
                              line_dash="dash", row=1, col=1,
                              annotation_text=f"SL {sl:.0f}",
                              annotation_font_color=C_SL)
            if tp:
                fig.add_hline(y=tp, line_color=C_TP, line_width=1.5,
                              line_dash="dash", row=1, col=1,
                              annotation_text=f"TP {tp:.0f}",
                              annotation_font_color=C_TP)
            # Trade entry marker
            if ep and "entry_ts" in trade:
                ets = pd.Timestamp(trade["entry_ts"])
                sym = "triangle-up" if trade.get("direction") == "bull" else "triangle-down"
                fig.add_trace(go.Scatter(
                    x=[ets], y=[ep],
                    mode="markers",
                    marker=dict(symbol=sym, size=14, color=C_ENTRY),
                    name="Entry",
                    showlegend=False,
                ), row=1, col=1)
            # Trade exit marker
            if "exit_price" in trade and "exit_ts" in trade:
                exts = pd.Timestamp(trade["exit_ts"])
                exp  = trade["exit_price"]
                fig.add_trace(go.Scatter(
                    x=[exts], y=[exp],
                    mode="markers",
                    marker=dict(symbol="x", size=14, color=C_SL),
                    name="Exit",
                    showlegend=False,
                ), row=1, col=1)

        # ── Layout ────────────────────────────────────────────────────────────
        now_str = pd.Timestamp.now().strftime("%H:%M:%S")
        fig.update_layout(
            title=dict(text=f"{title}   [{now_str} IST]", font=dict(size=14)),
            template="plotly_dark",
            xaxis_rangeslider_visible=False,
            height=700,
            margin=dict(l=50, r=50, t=60, b=40),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
            hovermode="x unified",
        )
        fig.update_xaxes(showgrid=True, gridcolor="rgba(255,255,255,0.08)")
        fig.update_yaxes(showgrid=True, gridcolor="rgba(255,255,255,0.08)")

        # Auto-refresh JS (every 60 seconds)
        auto_refresh = '<script>setTimeout(function(){location.reload();}, 60000);</script>'

        html_content = fig.to_html(full_html=True, include_plotlyjs="cdn")
        html_content = html_content.replace("</body>", f"{auto_refresh}</body>")

        self.output_path.write_text(html_content, encoding="utf-8")

    def get_path(self) -> str:
        return str(self.output_path)
