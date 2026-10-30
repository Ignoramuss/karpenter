"""Writes the Tier B KWOK catalog: the Linux entries of kwok/examples/aws_instance_types.json for the 15 types in
experiments/catalog.json, after checking each one's on-demand price against that catalog. Run from the repository
root:

    python3 experiments/tierb/env/instance_types.py experiments/tierb/env/instance_types.json
"""

import json
import sys

catalog = json.load(open("experiments/catalog.json"))
price = {c["name"]: c["price_per_hour"] for c in catalog}
full = json.load(open("kwok/examples/aws_instance_types.json"))
selected = [t for t in full if t["name"] in price and "linux" in t["operatingSystems"]]
assert sorted(t["name"] for t in selected) == sorted(price), "one Linux entry per catalog type"
for t in selected:
    on_demand = {o["Price"] for o in t["offerings"]
                 if any(r["key"] == "karpenter.sh/capacity-type" and "on-demand" in r["values"] for r in o["Requirements"])}
    assert len(on_demand) == 1 and abs(on_demand.pop() - price[t["name"]]) < 1e-9, t["name"]
json.dump(selected, open(sys.argv[1], "w"))
print(f"{len(selected)} Linux types; on-demand prices match experiments/catalog.json")
