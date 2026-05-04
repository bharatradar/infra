# Patch settings.py to add MY_DOMAIN env var
with open('/app/src/adsb_api/utils/settings.py', 'r') as f:
    settings_content = f.read()
if 'MY_DOMAIN' not in settings_content:
    settings_content = settings_content.replace(
        'INSECURE = os.getenv',
        'MY_DOMAIN = os.environ.get("MY_DOMAIN", "my.bharatradar.com")\nINSECURE = os.getenv'
    )
    with open('/app/src/adsb_api/utils/settings.py', 'w') as f:
        f.write(settings_content)
    print("settings.py patched successfully")

# Patch provider.py
with open('/app/src/adsb_api/utils/provider.py', 'r') as f:
    content = f.read()

if 'MY_DOMAIN' not in content:
    content = content.replace(
        'from adsb_api.utils.settings import (INGEST_DNS',
        'from adsb_api.utils.settings import (MY_DOMAIN, INGEST_DNS'
    )
    content = content.replace(
        '"adsblol_my_url": f"https://{_humanhash(c[0][:18], SALT_MY)}.my.adsb.lol"',
        '"bharatradar_my_url": f"https://{_humanhash(c[0][:18], SALT_MY)}.{MY_DOMAIN}"'
    )
    content = content.replace('"ip": c[1].split()[1]', '"ip": c[1].split()[0]')

    with open('/app/src/adsb_api/utils/provider.py', 'w') as f:
        f.write(content)
    print("provider.py patched successfully")

# Patch app.py
with open('/app/src/adsb_api/app.py', 'r') as f:
    content = f.read()

# Add MY_DOMAIN import if missing
if 'MY_DOMAIN,' not in content:
    content = content.replace(
        'from adsb_api.utils.settings import (INSECURE,',
        'from adsb_api.utils.settings import (INSECURE, MY_DOMAIN,'
    )

# Add startup event to force v2 route loading
if '_ = v2_router.routes' not in content and 'await provider.startup()' in content:
    content = content.replace('await provider.startup()\n', '''await provider.startup()
    _ = v2_router.routes  # Force v2 route discovery
    
    # Force routes into OpenAPI schema by accessing the routes property after include_router calls
    for route in app.routes:
        if hasattr(route, 'path'):
            _ = route.path
    
    # Also generate and discard the schema to force route registration
    _ = app.openapi()['paths']
''')

# Replace the /0/my route using string search (robust)
start = '@app.get("/0/my", tags=["v0"], summary="My Map redirect based on IP")'
end = 'return RedirectResponse(url=host)'

si = content.find(start)
if si >= 0:
    region = content[si:]
    # Find the LAST occurrence of the end marker (the function's final return)
    last_end = region.rfind(end)
    if last_end >= 0:
        old = region[:last_end + len(end)]
        new = '''def _get_client_ip(request: Request) -> str:
    x_real_ip = request.headers.get("X-Real-IP")
    if x_real_ip:
        return x_real_ip.split(",")[0].strip()
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.client.host


@app.get("/", include_in_schema=False)
async def my_root(request: Request):
    client_ip = _get_client_ip(request)
    all_clients = await provider._json_get("beast:clients") or []
    my_clients = [c for c in all_clients if c.get("ip") == client_ip]

    if len(my_clients) == 1:
        uuid = my_clients[0]["_uuid"][:18]
        return RedirectResponse(url=f"https://map.bharatradar.com/?filter_uuid={uuid}")

    if len(my_clients) > 1:
        return RedirectResponse(
            url=f"https://map.bharatradar.com/#sorry-but-i-could-not-find-your-receiver?"
        )

    if len(all_clients) == 1:
        uuid = all_clients[0]["_uuid"][:18]
        return RedirectResponse(url=f"https://map.bharatradar.com/?filter_uuid={uuid}")

    return RedirectResponse(url="https://map.bharatradar.com")


@app.get("/0/my", tags=["v0"], summary="My Map redirect based on IP")
@app.get("/api/0/my", tags=["v0"], summary="My Map redirect based on IP", include_in_schema=False)
async def api_my(request: Request):
    return RedirectResponse(url=f"https://map.bharatradar.com/")'''
        content = content.replace(old, new)
        print("app.py patched successfully (my_root route added)")
    else:
        print("WARNING: Could not find end of /0/my function")
else:
    print("WARNING: Could not find /0/my function to patch")

with open('/app/src/adsb_api/app.py', 'w') as f:
    f.write(content)

# Patch api_v2.py - fix broken decorator pattern
with open('/app/src/adsb_api/utils/api_v2.py', 'r') as f:
    content = f.read()

old_reapi = '''    def decorator(func):
        async def handler(request: Request, **path_kwargs) -> Response:
            actual_params = params(request) if callable(params) else params
            res = await provider.ReAPI.request(params=actual_params, client_ip=request.client.host)
            return Response(res, media_type="application/json")

        # Apply path param annotations if provided
        if path_params:
            for name, param in path_params.items():
                handler.__annotations__[name] = param

        # Register the route(s)
        for path in paths:
            router.get(path, summary=summary, description=description, **kwargs)(handler)

        return handler
    return decorator'''

new_reapi = '''    import inspect
    
    # Build signature params for OpenAPI
    sig_params = [inspect.Parameter('request', inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request)]
    
    # Create a handler that accepts path_kwargs internally but exposes only path params in signature
    async def _handler_impl(request: Request, **path_kwargs) -> Response:
        actual_params = params(request) if callable(params) else params
        res = await provider.ReAPI.request(params=actual_params, client_ip=request.client.host)
        return Response(res, media_type="application/json")
    
    # Build the public signature with explicit path params (no **kwargs)
    if path_params:
        for name, param in path_params.items():
            _handler_impl.__annotations__[name] = param
            sig_params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=param))
    
    _handler_impl.__signature__ = inspect.Signature(sig_params)
    
    # Wrapper that delegates to impl but has the clean signature
    async def handler(*args, **kwargs) -> Response:
        return await _handler_impl(*args, **kwargs)
    
    # Copy over the signature and annotations
    handler.__signature__ = _handler_impl.__signature__
    handler.__annotations__ = _handler_impl.__annotations__
    handler.__name__ = '_handler_impl'

    # Register the route(s)
    for path in paths:
        router.get(path, summary=summary, description=description, **kwargs)(handler)'''

if old_reapi in content:
    content = content.replace(old_reapi, new_reapi)
    with open('/app/src/adsb_api/utils/api_v2.py', 'w') as f:
        f.write(content)
    print("api_v2.py patched successfully")
else:
    print("WARNING: Could not find api_v2.py pattern to patch")

# Rebrand: Replace all adsb.lol references with bharatradar.com
for filepath in [
    '/app/src/adsb_api/app.py',
    '/app/README.md',
    '/app/src/adsb_api/utils/dependencies.py',
    '/app/src/adsb_api/utils/api_routes.py',
    '/app/src/adsb_api/utils/provider.py',
    '/app/src/adsb_api/utils/reapi.py',
]:
    try:
        with open(filepath, 'r') as f:
            content = f.read()
        original = content
        content = content.replace('adsb.lol', 'bharatradar.com')
        content = content.replace('BharatRadar API', 'BharatRadar API')
        content = content.replace('The BharatRadar API is a free and open source API for the BharatRadar project.', 'The BharatRadar API is a free and open source API for the BharatRadar project.')
        if content != original:
            with open(filepath, 'w') as f:
                f.write(content)
            print(f"Rebranded {filepath}")
    except FileNotFoundError:
        print(f"Skipping {filepath} (not found)")

print("All patches applied successfully")
