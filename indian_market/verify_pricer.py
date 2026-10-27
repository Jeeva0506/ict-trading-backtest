import sys; sys.path.insert(0, ".")
from backtest.option_pricer import next_expiry, dte_to_expiry, skew_adjusted_iv, select_strike
from datetime import date

tests = [
    date(2018, 5, 14),
    date(2019, 10, 21),
    date(2019, 10, 25),
    date(2024, 5, 13),
    date(2024, 5, 15),
    date(2024, 5, 16),
]
print("Expiry calendar check:")
for d in tests:
    exp = next_expiry(d)
    dte = dte_to_expiry(d)
    print(f"  {d} ({d.strftime('%a')}) -> expiry {exp} ({exp.strftime('%a')})  DTE={dte}")

print()
print("Strike + IV (Nifty spot=23750, VIX=14):")
spot, vix = 23750, 14.0
for direction in ("call", "put"):
    for itm in (0, 1):
        K  = select_strike(spot, direction, 50, itm)
        iv = skew_adjusted_iv(vix, direction, itm)
        label = "ATM" if itm == 0 else "1-ITM"
        mono  = "ITM" if (direction == "call" and K < spot) or (direction == "put" and K > spot) else "ATM"
        print(f"  {direction.upper()} {label}: K={K:.0f} ({mono})  IV={iv:.2f}%")
