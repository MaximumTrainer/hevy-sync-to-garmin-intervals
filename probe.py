import json, os, requests
s = requests.Session(); s.auth = ("API_KEY", os.environ["INTERVALS_API_KEY"])
A = "https://intervals.icu/api/v1/activity/i202190621"
def show(tag):
    d = s.get(f"{A}/intervals", timeout=30).json()
    iv = d["icu_intervals"]
    print(tag, "count", len(iv), "keys", sorted(iv[1].keys()))
    for i in iv[:4]:
        print(tag, json.dumps({k: i.get(k) for k in ("id", "start_index", "end_index", "type", "label", "intensity", "group_id")}))
    return iv
iv = show("before")
rest = iv[1]
r = s.put(f"{A}/intervals/{rest['id']}", json={**{k: rest[k] for k in ("start_index", "end_index", "label")}, "type": "RECOVERY"}, timeout=30)
print("single put", r.status_code, r.text[:300])
show("after-single")
