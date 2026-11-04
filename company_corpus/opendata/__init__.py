"""Layer 0 -> my-open-data batches (chantier 8): the producer side of the open-data store.

``submissions`` archives the EDGAR submissions API, ``identity`` and ``fundamentals`` map
layer 0 to contract rows (pure Python), ``batches`` writes them through the my-open-data
writer (optional extra ``company-corpus[store]``).
"""
