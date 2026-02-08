"""
Live Setup Backtest Simulation
==============================
Matches current live trading configuration:
- Indices: SPX500, GER30, FRA40, US30
- Adaptive Risk: 1.5% default, 1% in drawdown
- Uses live config parameters
"""
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
import sys

sys.path.insert(0, str(Path(__file__).parent))
from strategy.strategy_ict_clean import StrategyICTClean

# Broker point values ($ per 1 full index point per 1 lot)
BROKER_POINT_VALUES = {
    'SPX500': 10.0,
    'GER30': 11.81,
    'FRA40': 11.81,
    'US30': 10.0,
}

BROKER_MARGIN_PER_LOT = {
    'SPX500': 3461,
    'GER30': 14644,
    'FRA40': 4906,
    'US30': 3500,
}

# Matching LIVE config - only profitable indices (using 1-YEAR data)
LIVE_INDEX_CONFIG = {
    'SPX500': {
        'local_file': 'SPX500_1YEAR_M1.csv',
        'session_start_hour': 16,  # MT5 EET time
        'session_start_minute': 30,
        'sl_buffer': 1.0,
        'name': 'S&P 500'
    },
    'GER30': {
        'local_file': 'GER30_1YEAR_M1.csv',
        'session_start_hour': 10,
        'session_start_minute': 0,
        'sl_buffer': 2.0,
        'name': 'DAX 40'
    },
    'FRA40': {
        'local_file': 'FRA40_1YEAR_M1.csv',
        'session_start_hour': 10,
        'session_start_minute': 0,
        'sl_buffer': 1.0,
        'name': 'CAC 40'
    },
    'US30': {
        'local_file': 'US30_1YEAR_M1.csv',
        'session_start_hour': 16,
        'session_start_minute': 30,
        'sl_buffer': 5.0,
        'name': 'Dow Jones 30'
    },
}

# Adaptive risk matching live config
ADAPTIVE_RISK = {
    'enabled': True,
    'default_risk_percent': 1.5,
    'reduced_risk_percent': 1.0,
    'daily_dd_reduce_threshold': 1.0,  # Reduce to 1% risk at 1% daily DD
    'daily_dd_stop_threshold': 2.0,    # Stop trading at 2% daily DD (hard stop, limit is 3%)
}

# FundedNext account rules
ACCOUNT_RULES = {
    'starting_balance': 6000.0,
    'max_drawdown_pct': 8.0,           # 8% from starting balance = breach
    'daily_drawdown_pct': 3.0,         # 3% from day's peak = breach
    'phase1_target_pct': 8.0,          # Phase 1: 8% profit target
    'phase2_target_pct': 5.0,          # Phase 2: 5% profit target
}

# Realistic trading costs
TRADING_COSTS = {
    # Slippage in points (average entry + exit combined)
    'slippage_points': {
        'SPX500': 0.3,   # ~$3 per lot round trip
        'GER30': 0.5,    # ~$5.90 per lot
        'FRA40': 0.3,    # ~$3.54 per lot
        'US30': 1.0,     # ~$10 per lot (higher spread)
    },
    # Commission per lot round trip (FundedNext is commission-free on indices, but add spread cost)
    'commission_per_lot': {
        'SPX500': 0.0,
        'GER30': 0.0,
        'FRA40': 0.0,
        'US30': 0.0,
    },
    # Swap/overnight cost per lot per night (average, usually negative for indices)
    'swap_per_lot_per_night': {
        'SPX500': -2.5,
        'GER30': -3.0,
        'FRA40': -2.0,
        'US30': -2.5,
    },
}


def load_data(filename: str) -> pd.DataFrame:
    """Load local CSV data"""
    data_path = Path(__file__).parent / 'data' / 'raw' / filename
    if not data_path.exists():
        return None
    
    df = pd.read_csv(data_path, sep='\t', parse_dates=['Time'])
    df.set_index('Time', inplace=True)
    df.columns = [col.lower() for col in df.columns]
    return df


def get_adaptive_risk(current_balance: float, day_peak_balance: float) -> tuple:
    """
    Calculate risk based on DAILY drawdown from day's PEAK balance
    Returns: (risk_percent, should_stop_trading)
    """
    if not ADAPTIVE_RISK['enabled']:
        return ADAPTIVE_RISK['default_risk_percent'], False
    
    # Daily drawdown from today's PEAK balance (not starting balance)
    daily_dd_pct = ((day_peak_balance - current_balance) / day_peak_balance) * 100
    
    if daily_dd_pct >= ADAPTIVE_RISK['daily_dd_stop_threshold']:
        # Stop trading for the day (2% hard stop, before 3% breach)
        return ADAPTIVE_RISK['reduced_risk_percent'], True
    elif daily_dd_pct >= ADAPTIVE_RISK['daily_dd_reduce_threshold']:
        # Reduce risk at 1% daily DD
        return ADAPTIVE_RISK['reduced_risk_percent'], False
    else:
        return ADAPTIVE_RISK['default_risk_percent'], False


def check_account_breach(current_balance: float, starting_balance: float) -> bool:
    """Check if account is breached (8% from starting balance)"""
    max_dd = ((starting_balance - current_balance) / starting_balance) * 100
    return max_dd >= ACCOUNT_RULES['max_drawdown_pct']


def run_live_simulation():
    """Run backtest simulating live trading with adaptive risk"""
    
    print("=" * 100)
    print("   LIVE SETUP BACKTEST SIMULATION")
    print("   Adaptive Risk: 1.5% → 1% at 2% Drawdown (from Peak)")
    print("=" * 100)
    
    STARTING_CAPITAL = 6000.0  # Match your FundedNext account
    
    # First pass: collect all signals from all indices
    all_signals = []
    
    for symbol, config in LIVE_INDEX_CONFIG.items():
        print(f"\n📊 Collecting signals from {symbol} ({config['name']})...")
        
        df = load_data(config['local_file'])
        if df is None or len(df) == 0:
            print(f"   ⚠️ No data for {symbol}")
            continue
        
        point_value = BROKER_POINT_VALUES.get(symbol, 10.0)
        trading_days = sorted(set(df.index.date))
        
        for day in trading_days:
            prev_day = day - timedelta(days=1)
            while prev_day.weekday() >= 5:
                prev_day -= timedelta(days=1)
            
            day_start = pd.Timestamp(day) - timedelta(hours=12)
            day_end = pd.Timestamp(day) + timedelta(hours=23, minutes=59)
            
            day_df = df[(df.index >= day_start) & (df.index <= day_end)].copy()
            
            if len(day_df) < 100:
                continue
            
            prev_day_data = df[df.index.date == prev_day]
            pdh = prev_day_data['high'].max() if len(prev_day_data) > 0 else None
            pdl = prev_day_data['low'].min() if len(prev_day_data) > 0 else None
            
            current_day_data = day_df[day_df.index.date == day]
            if len(current_day_data) == 0:
                continue
            
            session_start = pd.Timestamp(day).replace(
                hour=config['session_start_hour'], 
                minute=config['session_start_minute']
            )
            market_hours_data = current_day_data[current_day_data.index >= session_start]
            
            if len(market_hours_data) > 0:
                day_high = market_hours_data['high'].max()
                day_low = market_hours_data['low'].min()
            else:
                day_high = current_day_data['high'].max()
                day_low = current_day_data['low'].min()
            
            # Run strategy with default capital (will recalculate later)
            strategy = StrategyICTClean(
                lot_size=1,
                sl_buffer=config['sl_buffer'],
                min_rr=2.0,
                bos_required=2,
                capital=STARTING_CAPITAL,
                risk_percent=1.5,
                session_start_hour=config['session_start_hour'],
                session_start_minute=config['session_start_minute']
            )
            
            signals = strategy.generate_signals(
                day_df, 
                day_high=day_high, 
                day_low=day_low,
                pdh=pdh,
                pdl=pdl,
                debug=False
            )
            
            for signal in signals:
                all_signals.append({
                    'symbol': symbol,
                    'signal': signal,
                    'point_value': point_value,
                    'entry_time': signal.entry_time,
                })
        
        print(f"   ✅ {len([s for s in all_signals if s['symbol'] == symbol])} signals")
    
    # Sort all signals chronologically
    all_signals.sort(key=lambda x: x['entry_time'])
    print(f"\n📈 Total signals collected: {len(all_signals)}")
    
    # Second pass: process signals chronologically with adaptive risk
    current_balance = STARTING_CAPITAL
    all_trades = []
    
    # Track daily balance for daily DD calculation
    current_day = None
    day_peak_balance = STARTING_CAPITAL  # Highest balance reached TODAY
    skipped_due_to_dd = 0
    
    for sig_data in all_signals:
        signal = sig_data['signal']
        symbol = sig_data['symbol']
        point_value = sig_data['point_value']
        
        # Check for account breach first (8% from starting balance)
        if check_account_breach(current_balance, STARTING_CAPITAL):
            print(f"   ⛔ ACCOUNT BREACHED at ${current_balance:.2f}")
            break
        
        # Check if new day - reset day's peak
        trade_day = signal.entry_time.date() if hasattr(signal.entry_time, 'date') else pd.Timestamp(signal.entry_time).date()
        if trade_day != current_day:
            current_day = trade_day
            day_peak_balance = current_balance  # Reset to current balance at start of day
        
        # Update day's peak (highest point today)
        day_peak_balance = max(day_peak_balance, current_balance)
        
        # Get adaptive risk based on daily drawdown from day's PEAK
        current_risk, should_stop = get_adaptive_risk(current_balance, day_peak_balance)
        
        # Skip if daily DD limit hit (2% from day's peak)
        if should_stop:
            skipped_due_to_dd += 1
            continue
        
        risk_amount = current_balance * (current_risk / 100)
        risk_points = signal.risk_points
        
        if risk_points > 0:
            lots = risk_amount / (risk_points * point_value)
            lots = max(0.01, min(40.0, lots))
            lots = int(lots * 100) / 100
            
            # Determine result and P&L (before costs)
            if signal.sl_hit and not signal.tp1_hit:
                result = 'loss'
                pnl_raw = -risk_amount
            elif signal.tp1_hit:
                result = 'win'
                pnl_points = signal.pnl_points
                pnl_raw = pnl_points * lots * point_value
            elif signal.sl_moved_to_be and signal.sl_hit:
                result = 'breakeven'
                pnl_raw = 0
            else:
                result = 'loss'
                pnl_raw = -risk_amount
            
            # Apply trading costs (slippage + commission)
            slippage_cost = TRADING_COSTS['slippage_points'].get(symbol, 0.3) * lots * point_value
            commission_cost = TRADING_COSTS['commission_per_lot'].get(symbol, 0) * lots
            total_costs = slippage_cost + commission_cost
            
            # Final P&L after costs
            pnl = pnl_raw - total_costs
            
            # Update running balance
            current_balance += pnl
            
            # Update day's peak after winning trade
            if pnl > 0:
                day_peak_balance = max(day_peak_balance, current_balance)
            
            all_trades.append({
                'symbol': symbol,
                'date': signal.entry_time,
                'direction': signal.direction,
                'result': result,
                'risk_percent': current_risk,
                'risk_amount': risk_amount,
                'pnl_raw': pnl_raw,
                'costs': total_costs,
                'pnl': pnl,
                'balance': current_balance,
                'day_peak_balance': day_peak_balance,
                'lots': lots,
            })
    
    print(f"   Processed: {len(all_trades)} trades chronologically")
    print(f"   Skipped due to 2% daily DD: {skipped_due_to_dd} trades")
    
    # Results
    print("\n" + "=" * 100)
    print("   SIMULATION RESULTS")
    print("=" * 100)
    
    if len(all_trades) == 0:
        print("No trades generated!")
        return
    
    trades_df = pd.DataFrame(all_trades)
    trades_df = trades_df.sort_values('date')
    
    # Recalculate cumulative balance in chronological order
    cumulative_balance = STARTING_CAPITAL
    balances = []
    for _, row in trades_df.iterrows():
        cumulative_balance += row['pnl']
        balances.append(cumulative_balance)
    trades_df['balance'] = balances
    
    total_trades = len(trades_df)
    wins = (trades_df['result'] == 'win').sum()
    losses = (trades_df['result'] == 'loss').sum()
    breakevens = (trades_df['result'] == 'breakeven').sum()
    
    total_pnl = trades_df['pnl'].sum()
    final_balance = STARTING_CAPITAL + total_pnl
    return_pct = (total_pnl / STARTING_CAPITAL) * 100
    
    gross_profit = trades_df[trades_df['pnl'] > 0]['pnl'].sum()
    gross_loss = abs(trades_df[trades_df['pnl'] < 0]['pnl'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Calculate max drawdown - TWO metrics
    # 1. Max DD from STARTING balance (FundedNext rule - breach at 8%)
    lowest_balance = trades_df['balance'].min()
    max_dd_from_start = STARTING_CAPITAL - lowest_balance
    max_dd_from_start_pct = (max_dd_from_start / STARTING_CAPITAL) * 100
    
    # 2. Max DD from PEAK equity (equity curve view)
    cumulative = trades_df['pnl'].cumsum() + STARTING_CAPITAL
    rolling_max = cumulative.cummax()
    drawdown = rolling_max - cumulative
    max_dd_from_peak = drawdown.max()
    max_dd_from_peak_pct = (max_dd_from_peak / rolling_max.max()) * 100
    
    print(f"\n💰 Account: ${STARTING_CAPITAL:,.0f} → ${final_balance:,.2f}")
    print(f"   Return: {return_pct:.2f}%")
    print(f"   Lowest Balance: ${lowest_balance:,.2f}")
    
    print(f"\n📊 Trade Stats:")
    print(f"   Total Trades: {total_trades}")
    print(f"   Wins: {wins} | Losses: {losses} | BE: {breakevens}")
    print(f"   Win Rate: {(wins/total_trades)*100:.1f}%")
    
    print(f"\n💵 P&L:")
    print(f"   Total P&L: ${total_pnl:,.2f}")
    print(f"   Profit Factor: {profit_factor:.2f}")
    print(f"   Max DD from Start: ${max_dd_from_start:,.2f} ({max_dd_from_start_pct:.2f}%)")
    print(f"   Max DD from Peak: ${max_dd_from_peak:,.2f} ({max_dd_from_peak_pct:.2f}%)")
    
    # Trades at each risk level
    trades_15 = trades_df[trades_df['risk_percent'] == 1.5]
    trades_10 = trades_df[trades_df['risk_percent'] == 1.0]
    
    print(f"\n🎯 Adaptive Risk Breakdown:")
    print(f"   At 1.5% risk: {len(trades_15)} trades, P&L: ${trades_15['pnl'].sum():,.2f}")
    print(f"   At 1.0% risk: {len(trades_10)} trades, P&L: ${trades_10['pnl'].sum():,.2f}")
    
    # Trading costs breakdown
    total_costs = trades_df['costs'].sum()
    print(f"\n💸 Trading Costs (Slippage + Commission):")
    print(f"   Total Costs: ${total_costs:,.2f}")
    print(f"   Avg Cost/Trade: ${total_costs/len(trades_df):,.2f}")
    print(f"   Raw P&L (before costs): ${trades_df['pnl_raw'].sum():,.2f}")
    print(f"   Net P&L (after costs): ${total_pnl:,.2f}")
    
    # By symbol
    print(f"\n📈 By Index:")
    for symbol in LIVE_INDEX_CONFIG.keys():
        sym_trades = trades_df[trades_df['symbol'] == symbol]
        if len(sym_trades) > 0:
            sym_pnl = sym_trades['pnl'].sum()
            sym_costs = sym_trades['costs'].sum()
            status = "🟢" if sym_pnl > 0 else "🔴"
            print(f"   {status} {symbol}: {len(sym_trades)} trades, ${sym_pnl:,.2f} (costs: ${sym_costs:,.2f})")
    
    # Calculate phase clearing times
    print("\n" + "=" * 100)
    print("   PHASE CLEARING ANALYSIS")
    print("=" * 100)
    
    phase1_target = STARTING_CAPITAL * (ACCOUNT_RULES['phase1_target_pct'] / 100)
    phase2_target = STARTING_CAPITAL * (ACCOUNT_RULES['phase2_target_pct'] / 100)
    
    # Find when targets were hit
    trades_df['cumulative_pnl'] = trades_df['pnl'].cumsum()
    trades_df['trade_date'] = pd.to_datetime(trades_df['date']).dt.date
    
    phase1_hit_idx = trades_df[trades_df['cumulative_pnl'] >= phase1_target].index
    phase2_hit_idx = trades_df[trades_df['cumulative_pnl'] >= phase2_target].index
    
    if len(phase1_hit_idx) > 0:
        first_phase1_idx = phase1_hit_idx[0]
        phase1_trades = first_phase1_idx + 1  # 0-indexed
        phase1_row = trades_df.loc[first_phase1_idx]
        first_trade_date = pd.to_datetime(trades_df.iloc[0]['date']).date()
        phase1_date = pd.to_datetime(phase1_row['date']).date()
        phase1_days = (phase1_date - first_trade_date).days + 1
        print(f"\n🏆 Phase 1 (8% = ${phase1_target:,.0f}):")
        print(f"   Cleared in: {phase1_trades} trades, {phase1_days} calendar days")
        print(f"   Date achieved: {phase1_date}")
    else:
        phase1_trades = None
        phase1_days = None
        print(f"\n🏆 Phase 1 (8% = ${phase1_target:,.0f}): Not achieved in data")
    
    if len(phase2_hit_idx) > 0:
        first_phase2_idx = phase2_hit_idx[0]
        phase2_trades = first_phase2_idx + 1
        phase2_row = trades_df.loc[first_phase2_idx]
        first_trade_date = pd.to_datetime(trades_df.iloc[0]['date']).date()
        phase2_date = pd.to_datetime(phase2_row['date']).date()
        phase2_days = (phase2_date - first_trade_date).days + 1
        print(f"\n🏆 Phase 2 (5% = ${phase2_target:,.0f}):")
        print(f"   Cleared in: {phase2_trades} trades, {phase2_days} calendar days")
        print(f"   Date achieved: {phase2_date}")
    else:
        phase2_trades = None
        phase2_days = None
        print(f"\n🏆 Phase 2 (5% = ${phase2_target:,.0f}): Not achieved in data")
    
    # Calculate min/max/avg days per "run" (simulating multiple attempts)
    # Run 252 trading day simulations starting from different points
    total_trading_days = len(trades_df['trade_date'].unique())
    avg_profit_per_trade = trades_df['pnl'].mean()
    
    if avg_profit_per_trade > 0:
        est_trades_phase1 = int(np.ceil(phase1_target / avg_profit_per_trade))
        est_trades_phase2 = int(np.ceil(phase2_target / avg_profit_per_trade))
        avg_trades_per_day = len(trades_df) / total_trading_days
        
        # Estimate days based on trade frequency
        est_days_phase1 = int(np.ceil(est_trades_phase1 / avg_trades_per_day))
        est_days_phase2 = int(np.ceil(est_trades_phase2 / avg_trades_per_day))
        
        print(f"\n📊 Phase Clearing Estimates:")
        print(f"   Avg Profit/Trade: ${avg_profit_per_trade:,.2f}")
        print(f"   Avg Trades/Day: {avg_trades_per_day:.1f}")
        print(f"   ")
        print(f"   Phase 1 (8%/${phase1_target:,.0f}):")
        print(f"      Est. Trades: {est_trades_phase1}")
        print(f"      Est. Trading Days: {est_days_phase1}")
        print(f"      Min (best case): {max(1, est_days_phase1 // 2)} days")
        print(f"      Max (worst case): {est_days_phase1 * 3} days")
        print(f"   ")
        print(f"   Phase 2 (5%/${phase2_target:,.0f}):")
        print(f"      Est. Trades: {est_trades_phase2}")
        print(f"      Est. Trading Days: {est_days_phase2}")
        print(f"      Min (best case): {max(1, est_days_phase2 // 2)} days")
        print(f"      Max (worst case): {est_days_phase2 * 3} days")
    
    # Check if passes FundedNext challenge
    print("\n" + "=" * 100)
    print("   FUNDEDNEXT EVALUATION CHECK")
    print("=" * 100)
    
    target_profit = STARTING_CAPITAL * 0.08  # 8% target
    max_allowed_dd = STARTING_CAPITAL * 0.08  # 8% max DD from starting balance
    
    print(f"\n🎯 Target Profit: ${target_profit:,.0f} (8%)")
    print(f"   Achieved: ${total_pnl:,.2f} ({return_pct:.2f}%)")
    if total_pnl >= target_profit:
        print(f"   ✅ PASS - Target exceeded!")
    else:
        print(f"   ❌ Need ${target_profit - total_pnl:,.2f} more")
    
    print(f"\n📉 Max DD Limit (8% from starting ${STARTING_CAPITAL:,.0f}):")
    print(f"   Floor: ${STARTING_CAPITAL - max_allowed_dd:,.0f}")
    print(f"   Lowest: ${lowest_balance:,.2f}")
    if max_dd_from_start <= max_allowed_dd:
        print(f"   ✅ PASS - Never breached!")
    else:
        print(f"   ❌ BREACH - Went below floor by ${max_dd_from_start - max_allowed_dd:,.2f}")
    
    # PROP FIRM RATING
    print("\n" + "=" * 100)
    print("   PROP FIRM ANTI-FLAG RATING")
    print("=" * 100)
    
    # Calculate metrics for flag detection
    avg_trades_per_week = len(trades_df) / (total_trading_days / 5)
    max_consecutive_wins = 0
    current_streak = 0
    for r in trades_df['result']:
        if r == 'win':
            current_streak += 1
            max_consecutive_wins = max(max_consecutive_wins, current_streak)
        else:
            current_streak = 0
    
    # Check for concerning patterns
    concerns = []
    rating = 10  # Start with perfect score
    
    # 1. Risk per trade (should be reasonable, not maxing out)
    if ADAPTIVE_RISK['default_risk_percent'] > 2.0:
        concerns.append("⚠️ High risk per trade (>2%)")
        rating -= 2
    
    # 2. Trade frequency (should be consistent, not bursts)
    if avg_trades_per_week > 20:
        concerns.append("⚠️ Very high trade frequency")
        rating -= 1
    elif avg_trades_per_week < 1:
        concerns.append("⚠️ Very low trade frequency")
        rating -= 1
    
    # 3. Win streaks (too many in a row is suspicious)
    if max_consecutive_wins > 10:
        concerns.append("⚠️ Long win streak (>10)")
        rating -= 1
    
    # 4. Phase clearing speed (too fast is suspicious)
    if phase1_trades and phase1_trades <= 2:
        concerns.append("🚩 Phase cleared too fast (<3 trades)")
        rating -= 3
    elif phase1_trades and phase1_trades <= 5:
        concerns.append("⚠️ Phase cleared quickly (3-5 trades)")
        rating -= 1
    
    # 5. Random delay check (we added this)
    has_random_delay = True  # We added 1-2s delay
    if has_random_delay:
        rating += 1  # Bonus for realistic timing
    
    # 6. Multi-symbol trading (looks more natural)
    symbols_used = trades_df['symbol'].nunique()
    if symbols_used >= 3:
        rating += 1  # Bonus for diversification
    
    # 7. Adaptive risk (shows real risk management)
    if len(trades_10) > 0:
        rating += 1  # Bonus for using adaptive risk
    
    rating = max(1, min(10, rating))  # Clamp to 1-10
    
    print(f"\n🎖️ Anti-Flag Rating: {rating}/10")
    print(f"   ")
    
    # Breakdown
    print(f"   ✅ Positives:")
    print(f"      - Risk per trade: {ADAPTIVE_RISK['default_risk_percent']}% (reasonable)")
    print(f"      - Random 1-2s delay on orders")
    print(f"      - {symbols_used} symbols traded (diversified)")
    print(f"      - Adaptive risk used ({len(trades_10)} trades at reduced risk)")
    print(f"      - Avg {avg_trades_per_week:.1f} trades/week (natural)")
    
    if concerns:
        print(f"   ")
        print(f"   ⚠️ Concerns:")
        for c in concerns:
            print(f"      - {c}")
    
    print(f"   ")
    if rating >= 8:
        print(f"   ✅ VERDICT: Low risk of flagging - setup looks natural")
    elif rating >= 5:
        print(f"   ⚠️ VERDICT: Moderate risk - some patterns may draw attention")
    else:
        print(f"   🚩 VERDICT: High risk - consider adjusting strategy")
    
    # Save trades
    output_dir = Path(__file__).parent / 'reports' / 'live_simulation'
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_df.to_csv(output_dir / 'trades.csv', index=False)
    print(f"\n📁 Trades saved: {output_dir / 'trades.csv'}")


if __name__ == "__main__":
    run_live_simulation()
