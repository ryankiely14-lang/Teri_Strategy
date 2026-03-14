"""
TeriStrategy Daily Report
Trade & Travel methodology — automated stock screening
Runs via GitHub Actions on weekdays at 9am Eastern
"""

import yfinance as yf
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
import os

# ── Watchlist ──────────────────────────────────────────────────────────────────
WATCHLIST = ["AAPL", "NVDA", "MSFT", "AMD", "GOOGL"]

# ── Email config (set these as GitHub Actions secrets) ─────────────────────────
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")   # Gmail App Password
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")


# ── Helpers ────────────────────────────────────────────────────────────────────

def days_to_earnings(ticker_obj):
    """Return days until next earnings, or None if unavailable."""
    try:
        cal = ticker_obj.calendar
        if cal is not None and "Earnings Date" in cal:
            dates = cal["Earnings Date"]
            if hasattr(dates, "__iter__"):
                for d in dates:
                    delta = (d.date() - datetime.today().date()).days
                    if delta >= 0:
                        return delta, d.strftime("%b %d")
    except Exception:
        pass
    return None, "Unknown"


def avg_daily_move(hist):
    """Average absolute daily price move over last 30 days."""
    if hist is None or len(hist) < 5:
        return 0
    return (hist["High"] - hist["Low"]).tail(30).mean()


def is_uptrend(hist):
    """
    Simple uptrend check over ~3 months (63 trading days):
    50-day SMA > 200-day SMA, and current price > 50-day SMA.
    Falls back gracefully if not enough data.
    """
    if hist is None or len(hist) < 50:
        return False
    close = hist["Close"]
    sma50  = close.tail(50).mean()
    sma200 = close.tail(200).mean() if len(close) >= 200 else close.mean()
    current = close.iloc[-1]
    return current > sma50 and sma50 > sma200


def room_to_run(current_price, week52_high):
    """
    How far (%) is current price below the 52-week high?
    Teri wants stocks that have pulled back but still have upside.
    > 15% below high = meaningful room to run.
    """
    if not week52_high or week52_high == 0:
        return None
    pct_below = ((week52_high - current_price) / week52_high) * 100
    return round(pct_below, 1)


def room_to_run_label(pct_below):
    """Convert % below 52-W high into a human-readable label."""
    if pct_below is None:
        return "Unknown", "❓"
    if pct_below < 5:
        return f"{pct_below}% below 52W high — near highs, limited upside", "⚠️"
    if pct_below < 15:
        return f"{pct_below}% below 52W high — modest room", "🟡"
    return f"{pct_below}% below 52W high — good room to run", "✅"


def calc_atr(hist, period=14):
    """
    Average True Range over last N days.
    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
    This is what Teri calls ATR — used to size the stop loss.
    """
    if hist is None or len(hist) < period + 1:
        return None
    h = hist["High"]
    l = hist["Low"]
    c = hist["Close"].shift(1)
    tr = (h - l).combine(
        (h - c).abs(), max
    ).combine(
        (l - c).abs(), max
    )
    return round(tr.tail(period).mean(), 2)


def find_support_resistance(hist, lookback=30):
    """
    Identify buyers level (support/demand zone) and sellers level
    (resistance/supply zone) from recent price action.

    Method: find the most significant recent swing low (buyers) and
    swing high (sellers) within the lookback window.
    These approximate the chart levels Teri draws manually.
    """
    if hist is None or len(hist) < lookback:
        return None, None

    recent = hist.tail(lookback)
    highs  = recent["High"]
    lows   = recent["Low"]

    # Sellers level: highest close cluster in the upper 25% of recent range
    range_high = highs.max()
    range_low  = lows.min()
    price_range = range_high - range_low
    if price_range == 0:
        return None, None

    upper_zone = range_high - (price_range * 0.25)
    lower_zone = range_low  + (price_range * 0.25)

    sellers_level = round(highs[highs >= upper_zone].mean(), 2)
    buyers_level  = round(lows[lows <= lower_zone].mean(), 2)

    return buyers_level, sellers_level


def calc_trade_setup(current_price, buyers_level, sellers_level, atr):
    """
    IWT trade setup worksheet calculation:
      Entry  = top of buyers level (the demand zone)
      Stop   = buyers_level bottom − (ATR × 20%)  [below the zone]
      Exit   = just below sellers level (the supply zone)
      Risk   = Entry − Stop
      Reward = Exit − Entry
      Ratio  = Reward / Risk  (need ≥ 3 to take the trade)

    Returns a dict with all fields, or None if data insufficient.
    """
    if not buyers_level or not sellers_level or not atr:
        return None
    if sellers_level <= buyers_level:
        return None

    entry  = round(buyers_level * 1.005, 2)          # slightly above buyers zone top
    stop   = round(buyers_level - (atr * 0.20), 2)   # ATR × 20% below buyers level
    exit_  = round(sellers_level * 0.995, 2)          # just below sellers zone

    risk   = round(entry - stop, 2)
    reward = round(exit_ - entry, 2)

    if risk <= 0:
        return None

    ratio  = round(reward / risk, 1)

    # IWT odds scoring (from the worksheet table)
    # We can score the ratio component automatically
    ratio_score = 2 if ratio >= 3 else (1 if ratio >= 2 else 0)

    # "In" score: how many candles has price been at/near this buyers level?
    # Approximate: if current price is already at/near entry, fewer candles = better
    pct_from_entry = abs(current_price - entry) / entry * 100
    in_score = 2 if pct_from_entry < 1 else (1 if pct_from_entry < 3 else 0)

    # Total automatable score (ratio + proximity); max = 4
    # Full score needs "Out" (exit speed) and "Fresh" (visits) — manual chart reads
    auto_score = ratio_score + in_score
    take_trade = ratio >= 3.0

    return {
        "entry":       entry,
        "stop":        stop,
        "exit":        exit_,
        "risk":        risk,
        "reward":      reward,
        "ratio":       ratio,
        "take_trade":  take_trade,
        "ratio_score": ratio_score,
        "in_score":    in_score,
        "auto_score":  auto_score,
        "atr":         atr,
    }


# ── Core analysis per ticker ───────────────────────────────────────────────────

def analyze(symbol):
    ticker = yf.Ticker(symbol)
    info   = ticker.info
    hist   = ticker.history(period="1y")

    if hist.empty:
        return {"symbol": symbol, "error": "No data"}

    current_price  = hist["Close"].iloc[-1]
    volume         = info.get("averageVolume", 0)
    week52_high    = info.get("fiftyTwoWeekHigh")
    week52_low     = info.get("fiftyTwoWeekLow")
    company_name   = info.get("longName", symbol)

    # ── IWT checklist ──────────────────────────────────────────────────────────
    passes_volume   = volume >= 1_000_000
    passes_price    = current_price >= 10
    uptrend         = is_uptrend(hist)
    avg_move        = avg_daily_move(hist)
    passes_movement = avg_move >= 1.0
    days_out, earn_date = days_to_earnings(ticker)
    near_earnings   = days_out is not None and days_out <= 5

    pct_below_high  = room_to_run(current_price, week52_high)
    rtr_text, rtr_icon = room_to_run_label(pct_below_high)

    # ── Position in 52-week range (for the worksheet "underline ▲" field) ──────
    if week52_high and week52_low and week52_high != week52_low:
        range_position = ((current_price - week52_low) / (week52_high - week52_low)) * 100
        range_label = f"{'▲' * int(range_position / 20 + 0.5)}  {range_position:.0f}% of 52W range"
    else:
        range_label = "Unknown"

    # ── IWT execution worksheet: ATR, support/resistance, trade setup ──────────
    atr = calc_atr(hist)
    buyers_level, sellers_level = find_support_resistance(hist)
    trade_setup = calc_trade_setup(current_price, buyers_level, sellers_level, atr)

    # ── Action label ───────────────────────────────────────────────────────────
    if near_earnings:
        action = "POST-EARNINGS WATCH"
    elif not passes_volume or not passes_price:
        action = "AVOID"
    elif not uptrend:
        action = "AVOID"
    elif not passes_movement:
        action = "BUILD WATCHLIST"
    elif pct_below_high is not None and pct_below_high < 5:
        action = "WAIT FOR PULLBACK"   # extended near highs — not enough room
    elif uptrend and passes_movement:
        action = "ACTIONABLE NOW"
    else:
        action = "WATCH FOR CONFIRMATION"

    return {
        "symbol":        symbol,
        "name":          company_name,
        "price":         round(current_price, 2),
        "volume":        f"{volume:,}",
        "avg_move":      round(avg_move, 2),
        "uptrend":       uptrend,
        "week52_high":   week52_high,
        "week52_low":    week52_low,
        "pct_below_high": pct_below_high,
        "rtr_text":      rtr_text,
        "rtr_icon":      rtr_icon,
        "range_label":   range_label,
        "earnings_date": earn_date,
        "days_to_earn":  days_out,
        "action":        action,
        "passes_volume": passes_volume,
        "passes_price":  passes_price,
        "passes_move":   passes_movement,
        "atr":           atr,
        "buyers_level":  buyers_level,
        "sellers_level": sellers_level,
        "trade_setup":   trade_setup,
    }


# ── Action label styling ───────────────────────────────────────────────────────

ACTION_STYLE = {
    "ACTIONABLE NOW":          ("🟢", "#1a7f37", "#d1fae5"),
    "WATCH FOR CONFIRMATION":  ("🔵", "#1d4ed8", "#dbeafe"),
    "WAIT FOR PULLBACK":       ("🟡", "#92400e", "#fef9c3"),
    "POST-EARNINGS WATCH":     ("🟠", "#9a3412", "#ffedd5"),
    "BUILD WATCHLIST":         ("⚪", "#374151", "#f3f4f6"),
    "AVOID":                   ("🔴", "#991b1b", "#fee2e2"),
}


# ── HTML report ────────────────────────────────────────────────────────────────

def build_html(results):
    date_str = datetime.today().strftime("%A, %B %d, %Y")

    cards = ""
    for r in results:
        if "error" in r:
            cards += f"<div class='card'><h2>{r['symbol']}</h2><p>Error: {r['error']}</p></div>"
            continue

        emoji, text_color, bg_color = ACTION_STYLE.get(r["action"], ("⚪", "#374151", "#f3f4f6"))

        # IWT checklist items
        def check(passed, label):
            icon = "✅" if passed else "❌"
            return f"<li>{icon} {label}</li>"

        checklist = f"""
        <ul class='checklist'>
          {check(r['passes_volume'],  f"Volume: {r['volume']} (need ≥1M)")}
          {check(r['passes_price'],   f"Price: ${r['price']} (need ≥$10)")}
          {check(r['uptrend'],        "3-month uptrend")}
          {check(r['passes_move'],    f"Avg daily move: ${r['avg_move']} (need ≥$1)")}
          {check(r['days_to_earn'] is None or r['days_to_earn'] > 5,
                 f"Earnings: {r['earnings_date']} ({r['days_to_earn']} days away)" if r['days_to_earn'] is not None else f"Earnings: {r['earnings_date']}")}
        </ul>
        """

        # 52-week range bar
        if r['week52_high'] and r['week52_low'] and r['week52_high'] != r['week52_low']:
            pct = ((r['price'] - r['week52_low']) / (r['week52_high'] - r['week52_low'])) * 100
            pct = max(2, min(98, pct))
            range_bar = f"""
            <div class='range-section'>
              <div class='range-label-row'>
                <span>52W Low: ${r['week52_low']:.2f}</span>
                <span>52W High: ${r['week52_high']:.2f}</span>
              </div>
              <div class='range-track'>
                <div class='range-fill' style='width:{pct:.0f}%'></div>
                <div class='range-marker' style='left:{pct:.0f}%'></div>
              </div>
              <div class='range-note'>{r['rtr_icon']} {r['rtr_text']}</div>
            </div>
            """
        else:
            range_bar = "<p style='color:#6b7280;font-size:13px'>52-week range unavailable</p>"

        # ── IWT execution worksheet block (ACTIONABLE NOW only) ──────────────
        trade_block = ""
        ts = r.get("trade_setup")
        if r["action"] == "ACTIONABLE NOW" and ts:
            take_color  = "#1a7f37" if ts["take_trade"] else "#991b1b"
            take_bg     = "#d1fae5" if ts["take_trade"] else "#fee2e2"
            take_label  = "✅ TAKE THE TRADE (ratio ≥ 3:1)" if ts["take_trade"] else "❌ SKIP — ratio below 3:1"
            trade_block = f"""
            <div class='trade-section'>
              <div class='trade-title'>📋 IWT Execution Worksheet</div>
              <div class='trade-grid'>
                <div class='trade-box'>
                  <div class='trade-label'>Sellers level (exit target)</div>
                  <div class='trade-val'>${r['sellers_level']:.2f}</div>
                </div>
                <div class='trade-box'>
                  <div class='trade-label'>Buyers level (entry zone)</div>
                  <div class='trade-val'>${r['buyers_level']:.2f}</div>
                </div>
                <div class='trade-box'>
                  <div class='trade-label'>Entry price</div>
                  <div class='trade-val'>${ts['entry']:.2f}</div>
                </div>
                <div class='trade-box'>
                  <div class='trade-label'>Stop price (ATR × 20%)</div>
                  <div class='trade-val red'>${ts['stop']:.2f}</div>
                </div>
                <div class='trade-box'>
                  <div class='trade-label'>Exit price</div>
                  <div class='trade-val green'>${ts['exit']:.2f}</div>
                </div>
                <div class='trade-box'>
                  <div class='trade-label'>ATR (14-day)</div>
                  <div class='trade-val'>${ts['atr']:.2f}</div>
                </div>
              </div>
              <div class='rr-row'>
                <span>Risk: <strong>${ts['risk']:.2f}</strong></span>
                <span>Reward: <strong>${ts['reward']:.2f}</strong></span>
                <span>Ratio: <strong>{ts['ratio']}:1</strong></span>
              </div>
              <div class='take-badge' style='color:{take_color};background:{take_bg}'>
                {take_label}
              </div>
              <div class='trade-note'>
                ⚠️ Verify buyers/sellers levels on your chart before trading.
                Stop and exit are computer-estimated — adjust to actual chart levels.
              </div>
            </div>
            """

        cards += f"""
        <div class='card'>
          <div class='card-header'>
            <div>
              <span class='symbol'>{r['symbol']}</span>
              <span class='company'>{r['name']}</span>
            </div>
            <span class='price'>${r['price']}</span>
          </div>

          <div class='action-badge' style='color:{text_color};background:{bg_color}'>
            {emoji} {r['action']}
          </div>

          {checklist}
          {range_bar}
          {trade_block}
        </div>
        """

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f9fafb; color: #111827; margin: 0; padding: 20px; }}
  .header {{ max-width: 680px; margin: 0 auto 24px; }}
  .header h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .header p  {{ color: #6b7280; font-size: 14px; margin: 0; }}
  .card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 12px;
           padding: 20px; margin: 0 auto 16px; max-width: 680px; }}
  .card-header {{ display: flex; justify-content: space-between; align-items: flex-start;
                  margin-bottom: 12px; }}
  .symbol  {{ font-size: 20px; font-weight: 700; margin-right: 8px; }}
  .company {{ font-size: 13px; color: #6b7280; }}
  .price   {{ font-size: 20px; font-weight: 600; }}
  .action-badge {{ display: inline-block; padding: 6px 14px; border-radius: 20px;
                   font-weight: 600; font-size: 13px; margin-bottom: 14px; }}
  .checklist {{ list-style: none; padding: 0; margin: 0 0 16px; font-size: 14px;
                line-height: 1.9; }}
  .range-section {{ border-top: 1px solid #f3f4f6; padding-top: 14px; }}
  .range-label-row {{ display: flex; justify-content: space-between;
                      font-size: 12px; color: #9ca3af; margin-bottom: 6px; }}
  .range-track {{ position: relative; height: 8px; background: #e5e7eb;
                  border-radius: 4px; margin-bottom: 6px; }}
  .range-fill  {{ position: absolute; height: 8px; background: #3b82f6;
                  border-radius: 4px; top: 0; left: 0; }}
  .range-marker {{ position: absolute; width: 14px; height: 14px;
                   background: #1d4ed8; border: 2px solid #fff;
                   border-radius: 50%; top: -3px; transform: translateX(-50%); }}
  .range-note {{ font-size: 13px; color: #374151; }}
  .trade-section {{ border-top: 1px solid #f3f4f6; padding-top: 16px; margin-top: 16px; }}
  .trade-title {{ font-weight: 600; font-size: 14px; margin-bottom: 12px; color: #111827; }}
  .trade-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin-bottom: 12px; }}
  .trade-box {{ background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px; }}
  .trade-label {{ font-size: 11px; color: #6b7280; margin-bottom: 4px; }}
  .trade-val {{ font-size: 16px; font-weight: 700; color: #111827; }}
  .trade-val.red {{ color: #991b1b; }}
  .trade-val.green {{ color: #1a7f37; }}
  .rr-row {{ display: flex; gap: 20px; font-size: 13px; color: #374151;
             background: #f3f4f6; padding: 10px 14px; border-radius: 8px; margin-bottom: 10px; }}
  .take-badge {{ display: inline-block; padding: 7px 16px; border-radius: 20px;
                 font-weight: 600; font-size: 13px; margin-bottom: 10px; }}
  .trade-note {{ font-size: 11px; color: #9ca3af; line-height: 1.5; }}
  .footer {{ text-align: center; color: #9ca3af; font-size: 12px; margin-top: 24px; }}
</style>
</head>
<body>
  <div class='header'>
    <h1>📈 TeriStrategy Daily Report</h1>
    <p>{date_str} — Trade &amp; Travel methodology</p>
  </div>
  {cards}
  <div class='footer'>
    TeriStrategy • Not financial advice • For educational use only
  </div>
</body>
</html>"""
    return html


# ── Email sender ───────────────────────────────────────────────────────────────

def send_email(html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📈 TeriStrategy Report — {datetime.today().strftime('%b %d')}"
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECEIVER
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
    print("✅ Email sent.")


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Running TeriStrategy scan — {datetime.today().strftime('%Y-%m-%d')}")
    results = [analyze(sym) for sym in WATCHLIST]

    for r in results:
        if "error" not in r:
            print(f"  {r['symbol']:6s} {r['action']:30s}  ${r['price']}  "
                  f"{r['rtr_icon']} {r['pct_below_high']}% below 52W high")

    html = build_html(results)

    # Save locally for testing
    with open("report_preview.html", "w") as f:
        f.write(html)
    print("📄 report_preview.html saved — open in browser to preview.")

    # Send email (requires env vars to be set)
    if EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECEIVER:
        send_email(html)
    else:
        print("⚠️  Email env vars not set — skipping send. Check your secrets.")
