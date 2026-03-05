import pandas as pd, numpy as np
for sym in ['XAUUSD','USDJPY','EURUSD']:
    df = pd.read_csv(f'reports/ob_v3_6mo_is_m3pivot/{sym}_v3_6mo_is_trades.csv')
    print(f'=== {sym} ===')
    longs = df[df["direction"]=="LONG"]["R"]
    shorts = df[df["direction"]=="SHORT"]["R"]
    print(f'Long WR: {(longs>0).mean():.1%}  Short WR: {(shorts>0).mean():.1%}')
    risk = abs(df['entry_price'] - df['sl'])
    print(f'Risk: mean={risk.mean():.4f}  median={risk.median():.4f}  min={risk.min():.4f}  max={risk.max():.4f}')
    print(f'Exit: {df.exit_reason.value_counts().to_dict()}')
    df['et'] = pd.to_datetime(df['entry_time'])
    df['xt'] = pd.to_datetime(df['exit_time'])
    df['mins'] = (df['xt']-df['et']).dt.total_seconds()/60
    print(f'Duration: mean={df.mins.mean():.0f}min  median={df.mins.median():.0f}min')
    # How many SL within 30min?
    fast_sl = df[(df.exit_reason.isin(["SL","SL_samebar"])) & (df.mins <= 30)]
    print(f'Fast SL (<30min): {len(fast_sl)} / {len(df)} = {len(fast_sl)/len(df):.1%}')
    print()
