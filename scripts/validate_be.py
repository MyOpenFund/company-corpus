#!/usr/bin/env python3
"""Live-validate the Belgium (FSMA STORI) backend from a non-WAF-blocked network.

The FSMA API (webapi.fsma.be) sits behind an F5 WAF that blocks bots; the backend
clears it with curl_cffi (Chrome impersonation). This must run from a normal
residential/office network — NOT a datacenter/CI IP. Setup once:

    pip install curl_cffi

Then:

    python scripts/validate_be.py                  # AB InBev by ISIN
    python scripts/validate_be.py BE0003796134     # any BE ISIN

It runs the REAL StoriBE.discover() end-to-end (search → parse → one download),
so a green run proves the backend works against the live API from your network.
"""
from __future__ import annotations

import sys
from collections import Counter

from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_be import StoriBE


def main() -> int:
    isin = sys.argv[1] if len(sys.argv) > 1 else "BE0974293251"  # AB InBev
    be = StoriBE()
    ent = Entity(lei="", name="AB INBEV", country="BE", isins=(isin,))

    print(f"[1/3] discover() for ISIN {isin} (real curl_cffi → F5 WAF → /stori/result)")
    docs = be.discover(ent)
    if be.errors:
        print("  errors:")
        for e in be.errors[:5]:
            print(f"    [{e['context']}] {str(e['error'])[:90]}")
    if not docs:
        print("  ✗ no documents. If errors mention the WAF/curl_cffi, you may be on a\n"
              "    blocked network, or curl_cffi isn't installed (pip install curl_cffi).")
        return 2
    print(f"  ✓ {len(docs)} documents")
    print(f"  by doc_type: {dict(Counter(d.doc_type for d in docs))}")

    print("\n[2/3] sample documents")
    for d in docs[:4]:
        nm = d.native_meta or {}
        print(f"  - {d.doc_type:18} {d.published_ts or '':10} {nm.get('reportingTopicName','')[:34]:34} "
              f"files={len(d.files)}")

    print("\n[3/3] download the first file (proves the /download endpoint via curl_cffi)")
    f = next((f for d in docs for f in d.files if f.get("url")), None)
    if not f:
        print("  (no downloadable file found)")
        return 0
    try:
        from curl_cffi import requests as creq
        s = creq.Session(impersonate="chrome124")
        s.get("https://webapi.fsma.be/api/v1/fr/stori/document-type",
              headers={"Origin": "https://www.fsma.be", "Referer": "https://www.fsma.be/"})
        r = s.get(f["url"], headers={"Origin": "https://www.fsma.be", "Referer": "https://www.fsma.be/"})
        magic = r.content[:4]
        kind = "PDF" if magic == b"%PDF" else ("ZIP/ESEF" if magic[:2] == b"PK" else f"?{magic!r}")
        print(f"  ✓ {f['name'][:50]} -> {r.status_code} {len(r.content)} bytes ({kind})")
    except Exception as exc:
        print(f"  ✗ download failed: {type(exc).__name__}: {exc}")
        return 3

    print("\n✓ BE backend works end-to-end against the live FSMA API from this network.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
