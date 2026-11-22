"""Layer 0 -> open-data-store batches (chantier 8): the producer side of the open-data store.

``submissions`` archives the EDGAR submissions API, ``statements`` the SEC bulk statement files;
``identity``, ``fundamentals`` and ``statements`` map layer 0 to contract rows (pure Python), ``batches`` writes them through the open-data-store
writer (optional extra ``company-corpus[store]``).
"""
