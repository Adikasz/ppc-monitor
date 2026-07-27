"""Elérhető hirdetési fiókok katalógusa — a `/account add` autocomplete forrása.

Cél: a fiók hozzáadásakor ne kelljen kézzel begépelni az `act_165789803008898`
vagy `123-456-7890` azonosítót — a felhasználó a fiók NEVÉRE gépelve válasszon.

Működés: a Meta (`/me/adaccounts`) és Google (`listAccessibleCustomers` +
`customer_client`) API-t hívjuk, az eredményt pedig platformonként rövid ideig
memóriában cache-eljük. A Discord az autocomplete-re ~3 másodpercen belül vár
választ, egy élő API hívás pedig másodperceket vehet igénybe — cache nélkül
minden billentyűleütés új hívást indítana.

FONTOS KORLÁT — a lista JAVASLAT, nem kizárólagos:
  A Meta `/me/adaccounts` csak a token tulajdonosához KÖZVETLENÜL rendelt
  fiókokat adja vissza. Éles mérésben 66 fiók jött vissza, miközben 71 Meta
  fiók van használatban — az ügynökségi/business hozzáférésű fiókok (pl.
  act_1846078935426937 / Életerő.info, 720 kampánnyal) kimaradnak, noha a
  kampányaikat a token látja. Ezért a `/account add` a KÉZI ID-beírást
  továbbra is elfogadja; az autocomplete csak megkönnyíti a gyakori esetet.
"""
from __future__ import annotations

import time
from typing import Any

from src.utils.logging import get_logger

log = get_logger(__name__)

# Ennyi ideig érvényes egy platform lekért fiók-listája (másodperc).
# Az autocomplete billentyűleütésenként hív — cache nélkül elfogyna a rate limit.
_CACHE_TTL_S = 300.0

# {platform: (lekérés_időpontja, fiókok)}
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _fetch_meta() -> list[dict[str, Any]]:
    from src.integrations.meta_ads import MetaAdsClient

    client = MetaAdsClient.get_instance()
    return client.list_accessible_accounts()


def _fetch_google() -> list[dict[str, Any]]:
    from src.integrations.google_ads import GoogleAdsClient

    client = GoogleAdsClient.get_instance()
    return client.list_accessible_accounts()


_FETCHERS = {"meta": _fetch_meta, "google": _fetch_google}


def get_accounts(platform: str, *, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Egy platform elérhető fiókjai (cache-elve).

    Bármilyen hiba (hiányzó token, nem telepített SDK, API hiba) esetén ÜRES
    listát ad — az autocomplete-nek sosem szabad kivétellel elszállnia, mert
    az a Discordban néma hibaként jelenne meg.
    """
    platform = (platform or "").strip().lower()
    fetcher = _FETCHERS.get(platform)
    if fetcher is None:
        return []

    now = time.monotonic()
    cached = _cache.get(platform)
    if not force_refresh and cached and (now - cached[0]) < _CACHE_TTL_S:
        return cached[1]

    try:
        accounts = fetcher()
    except Exception as exc:  # noqa: BLE001 — az autocomplete nem törhet el
        log.error("Fiók-katalógus lekérés hiba (%s): %s", platform, exc)
        # Lejárt cache is jobb, mint a semmi.
        return cached[1] if cached else []

    _cache[platform] = (now, accounts)
    return accounts


def search_accounts(
    platform: str,
    query: str,
    *,
    limit: int = 25,
    exclude_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Fiókok szűrése névre VAGY azonosítóra (kis/nagybetű-független részstring).

    Paraméterek:
        exclude_ids — már regisztrált fiókok, amiket nem ajánlunk fel újra
                      (a `/account add` így csak az újakat kínálja).
    """
    accounts = get_accounts(platform)
    if not accounts:
        return []

    q = (query or "").strip().lower()
    exclude = exclude_ids or set()

    matches = [
        a for a in accounts
        if str(a.get("id")) not in exclude
        and (not q or q in str(a.get("name", "")).lower() or q in str(a.get("id", "")).lower())
    ]
    matches.sort(key=lambda a: str(a.get("name", "")).lower())
    return matches[:limit]


def find_account_name(platform: str, account_id: str) -> str | None:
    """Egy adott fiók API-NEVE a katalógusból, ha ismert. None ha nincs (kézi eset).

    A `/account add` ezzel dönti el, hogy az `account:` mezőbe API-listás fiók
    került (van API-név → abból automatikus kliensnév és `ad_accounts.
    account_name`), vagy kézi azonosító (nincs API-név → kliensnév kötelező
    bekérése, lásd a modul-docstring korlátjait).

    A `account_id`-t a hívó normalizálja (`normalize_external_account_id`),
    ugyanolyan formában, mint a katalógus `id` mezője (act_... / csak számjegy).
    """
    account_id = str(account_id)
    for a in get_accounts(platform):
        if str(a.get("id")) == account_id:
            return a.get("name")
    return None


def invalidate(platform: str | None = None) -> None:
    """Cache ürítése (teszthez, vagy ha friss listát akarunk kényszeríteni)."""
    if platform is None:
        _cache.clear()
    else:
        _cache.pop((platform or "").strip().lower(), None)
