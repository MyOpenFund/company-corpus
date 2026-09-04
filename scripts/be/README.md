# scripts/be/

Developer aid for the optional `be` (Belgium / FSMA STORI) backend.

`capture_be_stori.py` records the real request/response shapes STORI returns
from behind its F5 BIG-IP ASM WAF, so the backend's HTML/JSON extractors can be
unit-tested against genuine fixtures instead of guessed ones. It must be run
from a non-datacenter network — see the script's own docstring for why and how.

The `be` backend itself is opt-in (`pip install '.[be]'`) and off by default;
it is not part of the default install or the default crawl surface. See the
project README's ["Fair access"](../../README.md#fair-access) section for why
this one backend is handled differently from the rest of the HTTP layer.
