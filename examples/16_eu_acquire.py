"""Bounded European acquisition demo (FR/DE/IT/ES). Network; set COMPANY_CORPUS_CONTACT.

    ./venv/bin/python examples/16_eu_acquire.py
"""
from company_corpus import Config, Fetcher
from company_corpus.eu.acquire import acquire

SEED = [  # Increment A: FR (API) + ES (scrape)
    {"name": "TotalEnergies SE", "country": "FR"},
    {"name": "LVMH Moet Hennessy Louis Vuitton SE", "country": "FR"},
    {"name": "Iberdrola SA", "country": "ES"},
    {"name": "Banco Santander SA", "country": "ES"},
]

cfg = Config()
summary = acquire(SEED, fetcher=Fetcher(cfg), config=cfg, download=True)
print(summary)
