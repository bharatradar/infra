"""Patch RedisVRS._loop to return False on failure (so retry is 60s, not 3600s)."""

provider_path = "/app/src/adsb_api/utils/provider.py"

with open(provider_path) as f:
    content = f.read()

# Find and replace the entire loop body
old = """        for name, url in (("route", "https://vrs-standing-data.adsb.lol/routes.csv.gz"), ("airport", "https://vrs-standing-data.adsb.lol/airports.csv.gz")):
            try:
                async with self._session.get(url) as r:
                    if r.status == 200:
                        pipe = self.redis.pipeline()
                        count = 0
                        for row in gzip.decompress(await r.read()).decode().splitlines():
                            pipe.set(f"vrs:{name}:{row.split(',')[0]}", row)
                            count += 1
                        await pipe.execute()
                        print(f"[RedisVRS._loop] {name}: {count} rows")
            except Exception as e:
                print(f"[RedisVRS._loop] Error fetching {name}: {e}")
                traceback.print_exc()
        return True"""

new = """        all_success = True
        for name, url in (("route", "https://vrs-standing-data.adsb.lol/routes.csv.gz"), ("airport", "https://vrs-standing-data.adsb.lol/airports.csv.gz")):
            try:
                async with self._session.get(url) as r:
                    if r.status == 200:
                        pipe = self.redis.pipeline()
                        count = 0
                        for row in gzip.decompress(await r.read()).decode().splitlines():
                            pipe.set(f"vrs:{name}:{row.split(',')[0]}", row)
                            count += 1
                        await pipe.execute()
                        print(f"[RedisVRS._loop] {name}: {count} rows")
                    else:
                        all_success = False
                        print(f"[RedisVRS._loop] {name}: HTTP {r.status}")
            except Exception as e:
                print(f"[RedisVRS._loop] Error fetching {name}: {e}")
                traceback.print_exc()
                all_success = False
        return all_success"""

if old in content:
    content = content.replace(old, new)
    with open(provider_path, "w") as f:
        f.write(content)
    print("Patched RedisVRS._loop to return False on failure")
elif "all_success" in content:
    print("RedisVRS._loop already patched")
else:
    print("WARNING: Could not match _loop body in provider.py")
    # Debug
    lines = content.split("\n")
    for i, l in enumerate(lines):
        if "for name, url in" in l and "vrs-standing-data" in l:
            print(f"Found at line {i+1}\nContext:\n" + "\n".join(lines[i:i+12]))
