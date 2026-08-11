"""ETF 종목명 해석이 신형 코드까지 되는지 실제 KRX로 확인."""
import sys
sys.path.insert(0, "src")
from threetv import market

codes = ["0197X0", "0193L0", "0198D0", "0197W0", "0192L0",
         "0194R0", "0194T0", "0193T0", "0195S0", "0194N0",
         "488080", "069500"]
m = market._etf_name_map()
print(f"맵 크기: {len(m)}")
for c in codes:
    print(f"  {c} -> {market.etf_name(c)}")
mv = market.kr_etf_top_movers(n=5)
print("\n오늘 ETF 등락 상위:")
for r in (mv.get("up") or []):
    print(f"  {r['ticker']}: {r['name']} {r['pct']:+.2f}%")
print("하위:")
for r in (mv.get("down") or []):
    print(f"  {r['ticker']}: {r['name']} {r['pct']:+.2f}%")
