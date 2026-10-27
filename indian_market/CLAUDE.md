Rules:
- Never introduce lookahead bias (all signals use prior bar's data)
- All P&L metrics are AFTER Zerodha transaction costs
- Capital: ₹10,00,000 | Risk per trade: 1% = ₹10,000
- Use synthetic option pricing (Black-Scholes) with India VIX as IV input
- Nifty lot size = 50, BankNifty lot size = 15
- Validate on 3yr first; if PF>2 and WR>55%, run full 10yr backtest
- No curve-fitting: optimize on 3yr, validate on 10yr (OOS)
- Strategy logic must be implementable in real-time without future data
