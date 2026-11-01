"""Read-only collectors for Social Pulse.

Only two kinds of network call exist in this file:
  * HTTP GET to https://api.x.com/2/...            (X public metrics)
  * HTTP GET/POST to https://api.cloudflare.com/client/v4/...
    - GET for nothing but metadata, POST only to /graphql with a `query{...}`
      (Cloudflare's GraphQL Analytics API is read-only; it has no mutations).
selfcheck.py fails the image build if anything else appears.
"""
import datetime as dt
import json
import re
import urllib.parse
import urllib.request

X_API = "https://api.x.com/2"
CF_API = "https://api.cloudflare.com/client/v4"
TIMEOUT = 20

NA_X_NONPUBLIC = ("X only returns this to the account owner's own user-context login; "
                  "an app bearer token can't read it")
NA_X_ACCOUNT_IMPRESSIONS = ("X has no account-level impressions or profile-visit endpoint for "
                            "app tokens; only per-post public impressions are summed here")


def ok(value):
    return {"value": value, "available": True}


def na(reason):
    return {"value": None, "available": False, "reason": reason}


def redact(text, secrets):
    text = str(text)
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9%._\-]+", r"\1***", text)


# ---------------------------------------------------------------- HTTP (read-only)
def _get(url, token):
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": "Bearer " + token, "User-Agent": "ha-social-pulse/0.1"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def _graphql(query, token):
    if not query.lstrip().startswith("query") or "mutation" in query.lower():
        raise ValueError("only read-only GraphQL queries are allowed")
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(CF_API + "/graphql", data=body, method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "User-Agent": "ha-social-pulse/0.1"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- X
def x_fetch_raw(handles, token, get=_get):
    q = urllib.parse.urlencode({"usernames": ",".join(handles),
                                "user.fields": "created_at,public_metrics,description,profile_image_url"})
    users = get(f"{X_API}/users/by?{q}", token)
    posts = {}
    for u in users.get("data", []):
        pq = urllib.parse.urlencode({"max_results": 20, "exclude": "retweets,replies",
                                     "tweet.fields": "created_at,public_metrics"})
        posts[u["id"]] = get(f"{X_API}/users/{u['id']}/tweets?{pq}", token)
    return {"users": users, "posts": posts}


def x_normalize(raw, handles, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    by_handle = {u["username"].lower(): u for u in raw.get("users", {}).get("data", [])}
    out = []
    for h in handles:
        u = by_handle.get(h.lower())
        if not u:
            out.append({"handle": h, "error": "account not returned by X (renamed, suspended or protected?)"})
            continue
        pm = u.get("public_metrics", {})
        plist = raw.get("posts", {}).get(u["id"], {}).get("data", []) or []
        rows, tot7 = [], {"impressions": 0, "likes": 0, "reposts": 0, "replies": 0, "posts": 0}
        for p in plist:
            m = p.get("public_metrics", {})
            created = dt.datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
            row = {"id": p["id"], "created_at": p["created_at"],
                   "text": (p.get("text") or "")[:140],
                   "url": p.get("url") or f"https://x.com/{u['username']}/status/{p['id']}",
                   "impressions": m.get("impression_count"), "likes": m.get("like_count"),
                   "reposts": m.get("retweet_count"), "replies": m.get("reply_count"),
                   "quotes": m.get("quote_count"), "bookmarks": m.get("bookmark_count")}
            rows.append(row)
            if (now - created).days < 7:
                tot7["posts"] += 1
                for k in ("impressions", "likes", "reposts", "replies"):
                    tot7[k] += row[k] or 0
        eng = tot7["likes"] + tot7["reposts"] + tot7["replies"]
        out.append({
            "handle": u["username"], "name": u.get("name"), "id": u["id"],
            "profile_image_url": u.get("profile_image_url"),
            "followers": ok(pm.get("followers_count")),
            "following": ok(pm.get("following_count")),
            "posts_total": ok(pm.get("tweet_count")),
            "posts_7d": ok(tot7["posts"]),
            "post_impressions_7d": ok(tot7["impressions"]),
            "engagements_7d": ok(eng),
            "engagement_rate_7d": ok(round(100.0 * eng / tot7["impressions"], 2)) if tot7["impressions"] else na("no impressions in the last 7 days"),
            "account_impressions": na(NA_X_ACCOUNT_IMPRESSIONS),
            "profile_visits": na(NA_X_NONPUBLIC),
            "link_clicks": na(NA_X_NONPUBLIC),
            "recent_posts": rows[:10],
        })
    return out


# ---------------------------------------------------------------- Cloudflare
def _zone_query(zone_id, d30, today, t7):
    return ('query{viewer{zones(filter:{zoneTag:"%s"}){'
            'daily:httpRequests1dGroups(limit:31,filter:{date_geq:"%s",date_leq:"%s"},orderBy:[date_ASC])'
            '{dimensions{date} sum{requests pageViews bytes threats countryMap{clientCountryName requests}} uniq{uniques}}'
            ' paths:httpRequestsAdaptiveGroups(limit:25,filter:{datetime_geq:"%s",requestSource:"eyeball"},orderBy:[count_DESC])'
            '{count dimensions{clientRequestPath}}}}}') % (zone_id, d30, today, t7)


def _rum_query(account_id, site_tag, d30):
    return ('query{viewer{accounts(filter:{accountTag:"%s"}){'
            'daily:rumPageloadEventsAdaptiveGroups(limit:40,filter:{siteTag:"%s",date_geq:"%s"},orderBy:[date_ASC]){count sum{visits} dimensions{date}}'
            ' pages:rumPageloadEventsAdaptiveGroups(limit:8,filter:{siteTag:"%s",date_geq:"%s"},orderBy:[count_DESC]){count dimensions{requestPath}}'
            ' refs:rumPageloadEventsAdaptiveGroups(limit:6,filter:{siteTag:"%s",date_geq:"%s"},orderBy:[count_DESC]){count dimensions{refererHost}}'
            '}}}') % (account_id, site_tag, d30, site_tag, d30, site_tag, d30)


def cf_fetch_raw(zones, account_id, token, now=None, gql=_graphql):
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    d30 = (now.date() - dt.timedelta(days=29)).isoformat()
    t7 = (now - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = {}
    for z in zones:
        entry = {"zone": gql(_zone_query(z["zone_id"], d30, today, t7), token)}
        if z.get("rum_site_tag") and account_id:
            entry["rum"] = gql(_rum_query(account_id, z["rum_site_tag"], d30), token)
        raw[z["name"]] = entry
    return raw


ASSET_RE = re.compile(r"(^/api/|^/cdn-cgi/)|\.(css|js|png|jpe?g|webp|gif|svg|ico|xml|txt|woff2?|map|json)$", re.I)
PROBE_RE = re.compile(r"(wp-|\.php|wlwmanifest|/\.env|/\.git|xmlrpc)", re.I)


def _gql_err(resp):
    errs = (resp or {}).get("errors")
    return "; ".join(e.get("message", "?") for e in errs) if errs else None


def cf_normalize(raw, zones, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    cut7 = (now.date() - dt.timedelta(days=6)).isoformat()
    out = []
    for z in zones:
        r = raw.get(z["name"], {})
        site = {"name": z["name"]}
        err = _gql_err(r.get("zone"))
        zd = None
        try:
            zd = r["zone"]["data"]["viewer"]["zones"][0]
        except (KeyError, IndexError, TypeError):
            pass
        if err or zd is None:
            reason = "Cloudflare zone analytics unreadable: " + (err or "no data (check token has Zone Analytics:Read for this zone)")
            for k in ("requests", "page_views", "uniques", "bandwidth_bytes", "threats"):
                site[k + "_7d"] = na(reason); site[k + "_30d"] = na(reason)
            site["daily"], site["countries_7d"], site["top_paths_7d"] = [], na(reason), na(reason)
        else:
            daily = [{"date": d["dimensions"]["date"], "requests": d["sum"]["requests"],
                      "page_views": d["sum"]["pageViews"], "uniques": d["uniq"]["uniques"],
                      "bytes": d["sum"]["bytes"], "threats": d["sum"]["threats"],
                      "countries": d["sum"].get("countryMap") or []} for d in zd.get("daily", [])]
            site["daily"] = [{k: v for k, v in d.items() if k != "countries"} for d in daily]
            first = daily[0]["date"] if daily else None
            site["data_since"] = first
            for label, rows in (("7d", [d for d in daily if d["date"] >= cut7]), ("30d", daily)):
                site["requests_" + label] = ok(sum(d["requests"] for d in rows))
                site["page_views_" + label] = ok(sum(d["page_views"] for d in rows))
                site["bandwidth_bytes_" + label] = ok(sum(d["bytes"] for d in rows))
                site["threats_" + label] = ok(sum(d["threats"] for d in rows))
                # Cloudflare's daily uniques can't be summed into a true period unique.
                site["uniques_" + label] = ok(sum(d["uniques"] for d in rows))
            c = {}
            for d in daily:
                if d["date"] >= cut7:
                    for cm in d["countries"]:
                        c[cm["clientCountryName"]] = c.get(cm["clientCountryName"], 0) + cm["requests"]
            site["countries_7d"] = ok(sorted(([k, v] for k, v in c.items()), key=lambda x: -x[1])[:8])
            paths = [[p["dimensions"]["clientRequestPath"], p["count"]] for p in zd.get("paths", [])]
            pages = [p for p in paths if not ASSET_RE.search(p[0]) and not PROBE_RE.search(p[0])]
            site["top_paths_7d"] = ok(pages[:8])
            site["bot_probe_requests_7d"] = ok(sum(p[1] for p in paths if PROBE_RE.search(p[0])))
        if "rum" not in r:
            reason = "Web Analytics site tag not configured" if not z.get("rum_site_tag") else "cloudflare_account_id not set"
            site["rum_pageloads_30d"] = na(reason); site["rum_visits_30d"] = na(reason)
            site["rum_top_pages"] = na(reason); site["rum_referrers"] = na(reason)
        else:
            err = _gql_err(r["rum"])
            try:
                acc = r["rum"]["data"]["viewer"]["accounts"][0]
            except (KeyError, IndexError, TypeError):
                acc = None
            if err or acc is None:
                reason = "Web Analytics unreadable: " + (err or "no data (token needs Account Analytics:Read)")
                site["rum_pageloads_30d"] = na(reason); site["rum_visits_30d"] = na(reason)
                site["rum_top_pages"] = na(reason); site["rum_referrers"] = na(reason)
            else:
                dl = acc.get("daily", [])
                site["rum_pageloads_30d"] = ok(sum(d["count"] for d in dl))
                site["rum_visits_30d"] = ok(sum(d["sum"]["visits"] for d in dl))
                site["rum_last_day"] = dl[-1]["dimensions"]["date"] if dl else None
                site["rum_top_pages"] = ok([[p["dimensions"]["requestPath"], p["count"]] for p in acc.get("pages", [])])
                site["rum_referrers"] = ok([[p["dimensions"]["refererHost"] or "(direct)", p["count"]] for p in acc.get("refs", [])])
        out.append(site)
    return out


def collect(opts, now=None):
    """Return the normalized snapshot. Never raises; failures become 'not available'."""
    now = now or dt.datetime.now(dt.timezone.utc)
    handles = opts.get("x_accounts") or []
    zones = opts.get("zones") or []
    secrets = [opts.get("x_bearer_token"), opts.get("cloudflare_api_token")]
    snap = {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "errors": []}
    xt = opts.get("x_bearer_token")
    if not xt:
        snap["x"] = [{"handle": h, "error": "x_bearer_token not set in add-on options"} for h in handles]
    else:
        try:
            snap["x"] = x_normalize(x_fetch_raw(handles, xt), handles, now)
        except Exception as e:  # noqa: BLE001
            msg = redact(e, secrets)
            snap["errors"].append("X: " + msg)
            snap["x"] = [{"handle": h, "error": "X API error: " + msg} for h in handles]
    ct = opts.get("cloudflare_api_token")
    if not ct:
        snap["sites"] = cf_normalize({}, zones, now)
        for s in snap["sites"]:
            s["error"] = "cloudflare_api_token not set in add-on options"
    else:
        try:
            snap["sites"] = cf_normalize(cf_fetch_raw(zones, opts.get("cloudflare_account_id"), ct, now), zones, now)
        except Exception as e:  # noqa: BLE001
            msg = redact(e, secrets)
            snap["errors"].append("Cloudflare: " + msg)
            snap["sites"] = cf_normalize({}, zones, now)
    return snap
