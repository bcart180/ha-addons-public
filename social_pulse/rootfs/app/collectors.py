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
_ID = re.compile(r"^[a-f0-9]{32}$")
_SCOPE = re.compile(r"auth|permiss|forbidden|unauthorized|not allowed|access denied", re.I)

# Edge uniques are per-day IP counts. Adding the days does not produce period uniques.
UNIQ_7 = "Sum of daily unique IPs. Not deduplicated across the 7 days."
UNIQ_30 = "Sum of daily unique IPs. Not deduplicated across the 30 days."

NA_X_NONPUBLIC = ("X only returns this to the account owner's own user-context login; "
                  "an app bearer token can't read it")
NA_X_ACCOUNT_IMPRESSIONS = ("X has no account-level impressions or profile-visit endpoint for "
                            "app tokens; only per-post public impressions are summed here")


def ok(value, **extra):
    item = {"value": value, "available": True}
    item.update(extra)
    return item


def na(reason, **extra):
    item = {"value": None, "available": False, "reason": reason}
    item.update(extra)
    return item


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


def _rum_query(account_id, site_tag, date_from, daily_limit):
    """Read-only Web Analytics window. date_from is inclusive (YYYY-MM-DD)."""
    return ('query{viewer{accounts(filter:{accountTag:"%s"}){'
            'daily:rumPageloadEventsAdaptiveGroups(limit:%d,filter:{siteTag:"%s",date_geq:"%s"},orderBy:[date_ASC]){count sum{visits} dimensions{date}}'
            ' pages:rumPageloadEventsAdaptiveGroups(limit:8,filter:{siteTag:"%s",date_geq:"%s"},orderBy:[count_DESC]){count dimensions{requestPath}}'
            ' refs:rumPageloadEventsAdaptiveGroups(limit:6,filter:{siteTag:"%s",date_geq:"%s"},orderBy:[count_DESC]){count dimensions{refererHost}}'
            '}}}') % (account_id, daily_limit, site_tag, date_from, site_tag, date_from, site_tag, date_from)


def _safe_gql(gql, query, token):
    """One read-only query. A failure becomes a GraphQL-shaped error and does not raise."""
    try:
        return gql(query, token)
    except Exception as e:  # noqa: BLE001
        return {"errors": [{"message": redact(e, [token])}]}


def _scope_error(err):
    return bool(err) and bool(_SCOPE.search(err))


def _rum_detail(err, scope):
    """Explain a Web Analytics miss. Ask to extend the existing token, not to replace it."""
    detail = err or "no account data"
    if scope or not err:
        return ("Sampled page visits and referrers are not readable (%s). "
                "Add Account → Account Analytics → Read to the existing Cloudflare API token. "
                "The token does not need to be replaced. Edge request metrics are unaffected."
                ) % detail
    return "Web Analytics unreadable: %s. Edge request metrics are unaffected." % detail


def cf_fetch_raw(zones, account_id, token, now=None, gql=_graphql):
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    d30 = (now.date() - dt.timedelta(days=29)).isoformat()
    d7 = (now.date() - dt.timedelta(days=6)).isoformat()
    # Adaptive top paths keep the existing rolling 7-day datetime filter.
    t7 = (now - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = {}
    for z in zones:
        entry = {}
        zid = z.get("zone_id") or ""
        if not _ID.match(zid):
            entry["zone"] = {"errors": [{"message": "zone_id must be 32 hex characters"}]}
        else:
            entry["zone"] = _safe_gql(gql, _zone_query(zid, d30, today, t7), token)
        tag = z.get("rum_site_tag") or ""
        if tag and account_id:
            if not _ID.match(tag) or not _ID.match(account_id):
                entry["rum"] = {"errors": [{"message": "Web Analytics site tag or account id is not 32 hex characters"}]}
                entry["rum7"] = {"errors": [{"message": "Web Analytics site tag or account id is not 32 hex characters"}]}
            else:
                entry["rum"] = _safe_gql(gql, _rum_query(account_id, tag, d30, 40), token)
                # Optional: a miss here must not drop the 30-day sample or the edge query.
                entry["rum7"] = _safe_gql(gql, _rum_query(account_id, tag, d7, 10), token)
        raw[z["name"]] = entry
    return raw


ASSET_RE = re.compile(r"(^/api/|^/cdn-cgi/)|\.(css|js|png|jpe?g|webp|gif|svg|ico|xml|txt|woff2?|map|json)$", re.I)
PROBE_RE = re.compile(r"(wp-|\.php|wlwmanifest|/\.env|/\.git|xmlrpc)", re.I)


def _gql_err(resp):
    errs = (resp or {}).get("errors")
    return "; ".join(e.get("message", "?") for e in errs) if errs else None


def _countries(rows):
    totals = {}
    for d in rows:
        for cm in d.get("countries") or []:
            name = cm.get("clientCountryName") or "?"
            totals[name] = totals.get(name, 0) + (cm.get("requests") or 0)
    return sorted(totals.items(), key=lambda x: -x[1])[:8]


def _window(label, part, whole, source):
    if not part.get("available") or not whole.get("available"):
        return {"label": label, "available": False, "source": source,
                "reason": part.get("reason") or whole.get("reason") or "not available"}
    if not whole["value"]:
        return {"label": label, "available": False, "source": source,
                "reason": "the 30-day total is 0, so a share of that window is undefined"}
    return {"label": label, "available": True, "source": source,
            "part": part["value"], "whole": whole["value"],
            "pct": round(100.0 * part["value"] / whole["value"], 1)}


def _prev7(daily, cut7, prior_start):
    """Prior calendar week, only when the returned edge series covers that whole window."""
    first = daily[0]["date"] if daily else None
    if not first or first > prior_start:
        return {"available": False, "source": "edge",
                "reason": "Daily edge history starts %s, which is after %s. The previous 7 days are not covered, so no change versus that window is shown."
                % (first or "unknown", prior_start)}
    prev = [d for d in daily if prior_start <= d["date"] < cut7]
    cur = [d for d in daily if d["date"] >= cut7]
    prior_last = (dt.date.fromisoformat(cut7) - dt.timedelta(days=1)).isoformat()

    def pack(key):
        prior = sum(d[key] for d in prev)
        current = sum(d[key] for d in cur)
        row = {"prior": prior, "current": current, "prior_start": prior_start, "prior_last": prior_last}
        if prior == 0:
            row["change_pct"] = None
            row["change_reason"] = "the prior 7-day total is 0, so percent change is undefined"
        else:
            row["change_pct"] = round(100.0 * (current - prior) / prior, 1)
        return row

    return {"available": True, "source": "edge", "requests": pack("requests"), "page_views": pack("page_views")}


def _rum_account(resp):
    err = _gql_err(resp)
    try:
        acc = resp["data"]["viewer"]["accounts"][0]
    except (KeyError, IndexError, TypeError):
        acc = None
    return acc, err


def _rum_daily(acc):
    rows = []
    for d in (acc or {}).get("daily") or []:
        rows.append({"date": d["dimensions"]["date"],
                     "pageloads": d.get("count") or 0,
                     "visits": ((d.get("sum") or {}).get("visits") or 0)})
    return rows


def _rum_pages(acc):
    return [[p["dimensions"].get("requestPath") or "/", p.get("count") or 0] for p in (acc or {}).get("pages") or []]


def _rum_refs(acc):
    return [[p["dimensions"].get("refererHost") or "(direct)", p.get("count") or 0] for p in (acc or {}).get("refs") or []]


def _fill_rum(site, reason, status, detail):
    for key in ("rum_pageloads_7d", "rum_pageloads_30d", "rum_visits_7d", "rum_visits_30d",
                "rum_top_pages", "rum_top_pages_7d", "rum_top_pages_30d",
                "rum_referrers", "rum_referrers_7d", "rum_referrers_30d"):
        site[key] = na(reason, source="web_analytics")
    site["rum_daily"] = []
    site["rum_last_day"] = None
    site["rum_status"] = status
    site["rum_status_detail"] = detail


def _apply_rum(site, raw_entry, zone, cut7):
    """30-day sample is required for sampled tiles. The 7-day query is optional."""
    if "rum" not in raw_entry:
        if not zone.get("rum_site_tag"):
            detail = ("Web Analytics site tag is not set for this zone, so sampled visits were not requested. "
                      "Edge request metrics do not use the tag.")
        else:
            detail = ("cloudflare_account_id is not set, so sampled visits were not requested. "
                      "Edge request metrics do not use the account id.")
        _fill_rum(site, detail, "not_configured", detail)
        return
    acc, err = _rum_account(raw_entry.get("rum"))
    if err or acc is None:
        scope = _scope_error(err) or not err
        detail = _rum_detail(err, scope)
        _fill_rum(site, detail, "missing_scope" if scope else "error", detail)
        return
    daily = _rum_daily(acc)
    pages = _rum_pages(acc)
    refs = _rum_refs(acc)
    loads, visits = sum(d["pageloads"] for d in daily), sum(d["visits"] for d in daily)
    site["rum_daily"] = daily
    site["rum_last_day"] = daily[-1]["date"] if daily else None
    site["rum_pageloads_30d"] = ok(loads, source="web_analytics")
    site["rum_visits_30d"] = ok(visits, source="web_analytics")
    site["rum_top_pages"] = ok(pages, source="web_analytics")
    site["rum_top_pages_30d"] = site["rum_top_pages"]
    site["rum_referrers"] = ok(refs, source="web_analytics")
    site["rum_referrers_30d"] = site["rum_referrers"]
    site["rum_status"] = "ok"
    site["rum_status_detail"] = ("Sampled page loads and visits are Web Analytics beacon events. "
                                 "They are not edge request counts.")
    acc7, err7 = _rum_account(raw_entry["rum7"]) if "rum7" in raw_entry else (None, None)
    if "rum7" in raw_entry and not err7 and acc7 is not None:
        d7 = _rum_daily(acc7)
        loads7, visits7 = sum(d["pageloads"] for d in d7), sum(d["visits"] for d in d7)
        site["rum_pageloads_7d"] = ok(loads7, source="web_analytics")
        site["rum_visits_7d"] = ok(visits7, source="web_analytics")
        site["rum_top_pages_7d"] = ok(_rum_pages(acc7), source="web_analytics")
        site["rum_referrers_7d"] = ok(_rum_refs(acc7), source="web_analytics")
        return
    sliced = [d for d in daily if d["date"] >= cut7]
    site["rum_pageloads_7d"] = ok(sum(d["pageloads"] for d in sliced), source="web_analytics",
                                  note="Summed from the 30-day daily sample. Not estimated from edge requests.")
    site["rum_visits_7d"] = ok(sum(d["visits"] for d in sliced), source="web_analytics",
                               note="Summed from the 30-day daily sample. Not estimated from edge requests.")
    if "rum7" not in raw_entry:
        why = "The 7-day Web Analytics query was not in this response."
    else:
        why = err7 or "no 7-day account data"
    list_reason = ("7-day sampled pages and referrers were not returned (%s). "
                   "7-day visit and page-load totals are summed from the 30-day daily sample, not from edge requests. "
                   "The 30-day sampled lists are unchanged.") % why
    site["rum_top_pages_7d"] = na(list_reason, source="web_analytics")
    site["rum_referrers_7d"] = na(list_reason, source="web_analytics")


def cf_normalize(raw, zones, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    cut7 = (now.date() - dt.timedelta(days=6)).isoformat()
    prior_start = (now.date() - dt.timedelta(days=13)).isoformat()
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
                site[k + "_7d"] = na(reason, source="edge")
                site[k + "_30d"] = na(reason, source="edge")
            site["daily"] = []
            site["countries_7d"] = na(reason, source="edge")
            site["countries_30d"] = na(reason, source="edge")
            site["top_paths_7d"] = na(reason, source="edge")
            site["bot_probe_requests_7d"] = na(reason, source="edge")
            site["prev7"] = {"available": False, "source": "edge", "reason": reason}
        else:
            daily = [{"date": d["dimensions"]["date"], "requests": d["sum"]["requests"],
                      "page_views": d["sum"]["pageViews"], "uniques": d["uniq"]["uniques"],
                      "bytes": d["sum"]["bytes"], "threats": d["sum"]["threats"],
                      "countries": d["sum"].get("countryMap") or []} for d in zd.get("daily", [])]
            site["daily"] = [{k: v for k, v in d.items() if k != "countries"} for d in daily]
            first = daily[0]["date"] if daily else None
            site["data_since"] = first
            week = [d for d in daily if d["date"] >= cut7]
            notes = {"7d": UNIQ_7, "30d": UNIQ_30}
            for label, rows in (("7d", week), ("30d", daily)):
                site["requests_" + label] = ok(sum(d["requests"] for d in rows), source="edge")
                site["page_views_" + label] = ok(sum(d["page_views"] for d in rows), source="edge",
                                                 note="Cloudflare edge HTML class, including bots. Not sampled browser visits.")
                site["bandwidth_bytes_" + label] = ok(sum(d["bytes"] for d in rows), source="edge")
                site["threats_" + label] = ok(sum(d["threats"] for d in rows), source="edge")
                # Summing daily unique IPs is not a deduplicated 7-day or 30-day unique.
                site["uniques_" + label] = ok(sum(d["uniques"] for d in rows), source="edge", note=notes[label])
            site["countries_7d"] = ok(_countries(week), source="edge")
            site["countries_30d"] = ok(_countries(daily), source="edge")
            paths = [[p["dimensions"]["clientRequestPath"], p["count"]] for p in zd.get("paths", [])]
            pages = [p for p in paths if not ASSET_RE.search(p[0]) and not PROBE_RE.search(p[0])]
            site["top_paths_7d"] = ok(pages[:8], source="edge",
                                      note="Edge requests over the rolling 7-day path query. Assets, /api, and WordPress probes are hidden.")
            site["bot_probe_requests_7d"] = ok(sum(p[1] for p in paths if PROBE_RE.search(p[0])), source="edge")
            site["prev7"] = _prev7(daily, cut7, prior_start)
        _apply_rum(site, r, z, cut7)
        site["windows"] = {
            "edge_requests": _window("Edge requests", site["requests_7d"], site["requests_30d"], "edge"),
            "edge_page_views": _window("Edge page views", site["page_views_7d"], site["page_views_30d"], "edge"),
            "rum_visits": _window("Sampled visits", site["rum_visits_7d"], site["rum_visits_30d"], "web_analytics"),
            "rum_pageloads": _window("Sampled page loads", site["rum_pageloads_7d"], site["rum_pageloads_30d"], "web_analytics"),
        }
        out.append(site)
    return out


def _mark_unavailable(site, reason):
    """Replace reasons on metrics we never fetched. Does not invent values."""
    for key, metric in list(site.items()):
        if isinstance(metric, dict) and metric.get("available") is False and "reason" in metric:
            metric["reason"] = reason
    prev = site.get("prev7")
    if isinstance(prev, dict) and not prev.get("available"):
        prev["reason"] = reason
    for row in (site.get("windows") or {}).values():
        if isinstance(row, dict) and not row.get("available"):
            row["reason"] = reason


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
        missing = "cloudflare_api_token not set in add-on options"
        for s in snap["sites"]:
            s["error"] = missing
            s["rum_status"] = "not_configured"
            s["rum_status_detail"] = missing + ". Sampled visits were not requested. Edge metrics are not estimated."
            _mark_unavailable(s, missing)
    else:
        try:
            snap["sites"] = cf_normalize(cf_fetch_raw(zones, opts.get("cloudflare_account_id"), ct, now), zones, now)
        except Exception as e:  # noqa: BLE001
            msg = redact(e, secrets)
            snap["errors"].append("Cloudflare: " + msg)
            snap["sites"] = cf_normalize({}, zones, now)
    return snap
