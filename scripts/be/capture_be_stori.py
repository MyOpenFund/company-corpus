#!/usr/bin/env python3
"""Capture real FSMA / STORI (Belgium) responses — run from a NON-datacenter network.

WHY THIS EXISTS
---------------
The FSMA's filing infrastructure sits behind an **F5 BIG-IP ASM WAF** that blocks
automated/non-browser HTTP clients. Two hosts matter:

  * stori.fsma.be       — the classic STORI search app (ASP.NET WebForms). The WAF
                          RESETS the connection for non-browser clients.
  * webapi.fsma.be      — a MODERN JSON API (Swagger-documented) that the FSMA's Vue
                          tools call (drupal setting `vueToolsApi`). The WAF returns
                          an F5 "Error Page … support ID: …" for non-browser clients.

Verified: the bare TLS handshake succeeds, but any HTTP request from curl /
python-`requests` — and even `curl_cffi` impersonating Chrome — is blocked **from a
datacenter/CI IP**. It is the client fingerprint *and* source-IP reputation together.
A normal residential/office browser passes. So this script must be run from YOUR
machine, and it impersonates a real Chrome fingerprint (curl_cffi) to satisfy the WAF.

WHAT IT DOES (in priority order)
  1. Pull the webapi.fsma.be OpenAPI spec (/swagger/v1/swagger.json). If this works it
     is the JACKPOT — it documents every STORI endpoint and the backend becomes a clean
     JSON-API client (like the UK NSM), no scraping.
  2. From the spec, probe a STORI search/issuer/document endpoint and save a sample.
  3. Fallback: capture the classic stori.fsma.be WebForms search + a result page.
Everything reachable is saved under tests/fixtures/eu/ and summarised on stdout.

SETUP
    pip install curl_cffi
RUN
    python scripts/be/capture_be_stori.py
Then commit the new tests/fixtures/eu/be_* files (or paste the printed summary back).

If every request shows "F5-WAF-BLOCK": confirm https://www.fsma.be/fr/stori and the
STORI search load in your normal browser. If they do but this can't, open the browser
DevTools → Network tab, run a STORI search, and paste the request URL(s) that hit
webapi.fsma.be — that is all you need to provide.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

FIX = pathlib.Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "eu"
WEBAPI = "https://webapi.fsma.be"
STORI = "https://stori.fsma.be"
SWAGGER_SPEC = WEBAPI + "/swagger/v1/swagger.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_IMPERSONATE = ["chrome124", "chrome120", "chrome", "safari17_0", "edge122"]

try:
    from curl_cffi import requests as _curl
    _HAVE_CURL = True
except ImportError:  # pragma: no cover
    _HAVE_CURL = False
try:
    import requests as _plain
except ImportError:  # pragma: no cover
    _plain = None


def _is_f5_block(text: str) -> bool:
    return ("support ID" in text or "Error Page" in text or "Website error" in text)


def _save(name: str, content) -> pathlib.Path:
    FIX.mkdir(parents=True, exist_ok=True)
    p = FIX / name
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return p


def _open_session():
    """Return (session, label) for whichever client clears the F5 WAF, else (None,None)."""
    if _HAVE_CURL:
        for imp in _IMPERSONATE:
            try:
                s = _curl.Session(impersonate=imp, timeout=30)
                s.headers.update({"Accept-Language": "en,fr,nl"})
                r = s.get(WEBAPI + "/swagger/index.html", timeout=30)
                if r.status_code < 500 and not _is_f5_block(r.text):
                    print(f"  ✓ WAF cleared via curl_cffi impersonate={imp}")
                    return s, f"curl_cffi:{imp}"
                print(f"  · curl_cffi {imp}: reachable but F5-WAF-BLOCK")
            except Exception as exc:
                print(f"  · curl_cffi {imp}: {type(exc).__name__} — {str(exc)[:55]}")
    if _plain is not None:
        try:
            s = _plain.Session()
            s.headers.update({"User-Agent": UA, "Accept-Language": "en,fr,nl"})
            r = s.get(WEBAPI + "/swagger/index.html", timeout=30)
            if not _is_f5_block(r.text):
                print("  ✓ WAF cleared via plain requests")
                return s, "requests"
            print("  · plain requests: F5-WAF-BLOCK")
        except Exception as exc:
            print(f"  · plain requests: {type(exc).__name__} — {str(exc)[:55]}")
    return None, None


def _hidden_fields(html: str) -> dict[str, str]:
    out = {}
    for tag in re.findall(r'<input[^>]*type="hidden"[^>]*>', html, re.I):
        n = re.search(r'name="([^"]+)"', tag)
        v = re.search(r'value="([^"]*)"', tag)
        if n:
            out[n.group(1)] = v.group(1) if v else ""
    return out


def _named_inputs(html: str) -> list[str]:
    return sorted({n for n in re.findall(r'<(?:input|select|textarea)[^>]*name="([^"]+)"', html, re.I)})


def _form_action(html: str) -> str:
    m = re.search(r'<form[^>]+action="([^"]+)"', html, re.I)
    if not m:
        return "/Search.aspx"
    a = m.group(1).lstrip(".")
    return a if a.startswith("/") else "/" + a


def _find(names, *needles):
    for n in names:
        low = n.lower()
        if all(x in low for x in needles):
            return n
    return None


def _capture_webapi(s) -> bool:
    """Try the modern JSON API. Returns True if the OpenAPI spec was captured."""
    print(f"\n[A] webapi.fsma.be JSON API — fetch OpenAPI spec\n    GET {SWAGGER_SPEC}")
    try:
        r = s.get(SWAGGER_SPEC, timeout=40)
    except Exception as exc:
        print(f"  ✗ {type(exc).__name__}: {exc}")
        return False
    if _is_f5_block(r.text) or r.text.lstrip()[:1] not in "{[":
        print("  ✗ F5-WAF-BLOCK / not JSON — the spec is gated for this client.")
        return False
    sp = _save("be_webapi_swagger.json", r.text)
    print(f"  ✓ JACKPOT — OpenAPI spec captured → {sp}")
    try:
        spec = json.loads(r.text)
    except Exception:
        return True
    paths = spec.get("paths", {})
    print(f"  API title: {spec.get('info', {}).get('title')} | {len(paths)} paths")
    relevant = [p for p in sorted(paths)
                if re.search(r"stori|document|issuer|compan|regulated|search|file|emit|filing", p, re.I)]
    print("  STORI-relevant endpoints:")
    for p in relevant[:40]:
        methods = ",".join(m.upper() for m in paths[p] if m in ("get", "post", "put"))
        print(f"    {methods:8} {p}")
    # Try a couple of GET endpoints that need no params, to capture a sample shape.
    for p in relevant:
        ops = paths[p]
        if "get" in ops and "{" not in p:
            url = WEBAPI + p
            try:
                rr = s.get(url, timeout=30)
                if not _is_f5_block(rr.text):
                    fn = "be_webapi_sample_" + re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-")[:40] + ".json"
                    _save(fn, rr.text)
                    print(f"    sample {p} -> {rr.status_code} saved {fn} ({len(rr.text)}b)")
                    break
            except Exception:
                pass
    return True


def _capture_stori_webforms(s, company: str, isin: str) -> None:
    """Fallback: the classic stori.fsma.be ASP.NET WebForms search."""
    print(f"\n[B] stori.fsma.be WebForms fallback\n    GET {STORI}/")
    try:
        r = s.get(STORI + "/", timeout=30)
    except Exception as exc:
        print(f"  ✗ {type(exc).__name__}: {exc}")
        return
    if _is_f5_block(r.text):
        print("  ✗ F5-WAF-BLOCK on stori.fsma.be too.")
        return
    form = r.text
    _save("be_stori_search.html", form)
    action, names, hidden = _form_action(form), _named_inputs(form), _hidden_fields(form)
    company_field = _find(names, "company", "text") or _find(names, "companyname")
    isin_field = _find(names, "isin")
    button = _find(names, "search", "button") or _find(names, "btnsearch") or _find(names, "searchbutton")
    print(f"  form={action} company={company_field} isin={isin_field} button={button}")
    body = dict(hidden)
    if isin_field and isin:
        body[isin_field] = isin
    elif company_field:
        body[company_field] = company
    if button:
        body[button] = "Search"
    try:
        rr = s.post(STORI + action, data=body, timeout=40)
        _save("be_stori_result.html", rr.text)
        links = sorted(set(re.findall(
            r'(?:href|src)="([^"]*(?:ViewDocument|Document|Download|\.pdf|\.zip)[^"]*)"', rr.text, re.I)))
        print(f"  result saved ({len(rr.text)}b); {len(links)} document links")
        for d in links[:8]:
            print(f"    {d[:110]}")
    except Exception as exc:
        print(f"  ✗ search POST failed: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default="Anheuser-Busch InBev")
    ap.add_argument("--isin", default="BE0974293251")
    args = ap.parse_args()

    if not _HAVE_CURL:
        print("! curl_cffi not installed — the WAF blocks plain requests by fingerprint.")
        print("  Run:  pip install curl_cffi   then re-run this script.\n")

    print("[0] open a WAF-clearing session")
    s, via = _open_session()
    if s is None:
        print("\n✗ Every client was blocked by the F5 WAF from this machine.")
        print("  1) pip install curl_cffi  and re-run (real browser fingerprint).")
        print("  2) confirm https://www.fsma.be/fr/stori opens in your browser.")
        print("  3) if the browser works: DevTools → Network → run a STORI search →")
        print("     paste the webapi.fsma.be request URL(s). That's all that's needed.")
        return 2
    print(f"  session: {via}")

    got_spec = _capture_webapi(s)
    if not got_spec:
        _capture_stori_webforms(s, args.company, args.isin)

    print("\n────────────────────────────────────────────────────────────")
    print("DONE. Commit the new tests/fixtures/eu/be_* files (or paste the")
    print("summary above). With the OpenAPI spec or a real result page, the")
    print("BE backend is one validated build cycle away.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
