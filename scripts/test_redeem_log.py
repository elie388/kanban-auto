import sys
sys.path.insert(0, '.')
from datetime import datetime
import sync_sections as ss

LEDGER = """# 积分记录
## 五年级
| 2026-10-18 | 兑换30分钟Minecraft | -70 | 168 | <!-- key:五年级_2026-10-18_redeem_minecraft30_20261018a --> |
| 2026-10-12 | 兑换1小时Minecraft | -100 | 111 | <!-- key:五年级_2026-10-12_redeem_minecraft60_20261012a --> |
| 2026-10-12 | 兑换手续费（上学日15%） | -15 | 96 | <!-- key:五年级_2026-10-12_redeem_minecraft60_20261012a_fee --> |
| 2026-10-08 | 兑换1小时和平精英 | -150 | 300 | <!-- key:五年级_2026-10-08_redeem_heping150_20261008a --> |
| 2026-10-08 | 撤销兑换：选错档位 | +150 | 450 | <!-- key:五年级_2026-10-08_redeem_heping150_20261008a_reversal --> |
| 2026-09-30 | 兑换30分钟Minecraft | -70 | 200 | <!-- key:五年级_2026-09-30_redeem_minecraft30_20260930a --> |
| 2026-10-20 | 洗碗 | +5 | 455 | <!-- key:五年级_2026-10-20_chore_洗碗 --> |
## 六年级
| 2026-10-20 | 53天天练 | +3 | 394 | <!-- key:六年级_2026-10-20_53 --> |
## 待终审登记
"""
now = datetime(2026, 10, 21, 12, 0, tzinfo=ss.BEIJING)
out = ss.build_redeem_log(LEDGER, now=now)
g5, g6 = out['by_grade']['g5'], out['by_grade']['g6']
ok = []
ok.append(('窗口内3条（9-30超15天排除）', len(g5) == 3))
ok.append(('倒序', [r['date'] for r in g5] == ['2026-10-18', '2026-10-12', '2026-10-08']))
r0 = g5[0]
ok.append(('普通条：花费70/费0/兑后168/名', r0['spent'] == 70 and r0['fee'] == 0 and r0['balance_after'] == 168 and r0['item'] == '30分钟Minecraft'))
r1 = g5[1]
ok.append(('手续费归并：花费115/费15/兑后96', r1['spent'] == 115 and r1['fee'] == 15 and r1['balance_after'] == 96))
r2 = g5[2]
ok.append(('撤销：revoked/退150/原因', r2['revoked'] is True and r2['refunded'] == 150 and '选错档位' in r2['revoke_reason']))
ok.append(('六年级空', g6 == []))
ok.append(('非兑换行不入', all(r['item'] != '洗碗' for r in g5)))
for name, res in ok:
    print(('PASS' if res else 'FAIL'), name)
print('ALL PASS' if all(r for _, r in ok) else 'SOME FAIL')
import json; print(json.dumps(out, ensure_ascii=False)[:300])
