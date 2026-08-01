"""Курсы валют (FX) с суточным кешем — для конвертации зарплат в ленте.

Источник: open.er-api.com (бесплатно, без ключа, суточные курсы, base USD).
Кеш data/fx_rates.json (TTL 24ч): сеть только при протухшем кеше; при сбое — старый кеш
или хардкод-фолбэк (лента не должна падать из-за FX).
"""
import json
import shutil
import subprocess
import time

from hrwork.config import DATA_DIR, log

FX_CACHE_FILE = DATA_DIR / "fx_rates.json"
FX_TTL_HOURS = 24
FX_API = "https://open.er-api.com/v6/latest/USD"
CURL = shutil.which("curl") or "curl"

# Алиасы валют -> канонический код курса (per-USD): RUR(HH)=RUB, BYR(старый бел.)=BYN, USDT=USD.
CURRENCY_ALIAS = {"RUR": "RUB", "BYR": "BYN", "USDT": "USD"}

# Грубый фолбэк, если ни API, ни кеша (per-USD) — чтобы лента работала офлайн.
_FALLBACK = {"USD": 1.0, "EUR": 0.92, "RUB": 90.0, "BYN": 3.3, "GBP": 0.79,
             "CAD": 1.37, "PLN": 4.0, "AUD": 1.5}


def resolve_currency(code: str | None) -> str:
    """Код валюты -> канонический (RUR->RUB, USDT->USD, BYR->BYN). Пусто/None -> ''."""
    c = (code or "").upper()
    return CURRENCY_ALIAS.get(c, c)


def to_rub(amount: float | None, currency: str | None,
           rates: dict[str, float] | None = None) -> int | None:
    """Сумму в валюте -> RUB по курсам per-USD. Неизвестная валюта/нет курса -> None.
    Пустая валюта трактуется как RUB (HH по умолчанию рублёвый)."""
    if amount is None:
        return None
    r = rates if rates is not None else get_rates()
    cur = resolve_currency(currency) or "RUB"
    rate_from, rate_rub = r.get(cur), r.get("RUB")
    if not rate_from or not rate_rub:
        return None
    return round(amount / rate_from * rate_rub)


def _fetch() -> dict[str, float] | None:
    """Свежие курсы per-USD из API (dict) или None при сбое/невалидном ответе."""
    try:
        out = subprocess.run([CURL, "-s", "--max-time", "15", FX_API],
                             capture_output=True, timeout=20).stdout
        data = json.loads(out or b"{}")
        if data.get("result") == "success" and data.get("rates"):
            return {k: float(v) for k, v in data["rates"].items()}
    except Exception as e:
        log.warning("FX-курсы не получены: {}", e)
    return None


def get_rates() -> dict[str, float]:
    """Курсы per-USD (USD=1.0). Свежий кеш (<24ч) -> из файла; иначе фетч + перезапись кеша;
    при сбое фетча -> старый кеш, иначе фолбэк. USDT-алиас на JS-стороне (=USD)."""
    if FX_CACHE_FILE.exists():
        try:
            cached = json.loads(FX_CACHE_FILE.read_text(encoding="utf-8"))
            age_h = (time.time() - cached.get("fetched_at", 0)) / 3600
            if age_h < FX_TTL_HOURS and cached.get("rates"):
                fresh: dict[str, float] = cached["rates"]
                return fresh
        except (json.JSONDecodeError, OSError):
            pass

    rates = _fetch()
    if rates:
        rates.setdefault("USD", 1.0)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = FX_CACHE_FILE.with_name(FX_CACHE_FILE.name + ".tmp")
        tmp.write_text(json.dumps({"fetched_at": time.time(), "rates": rates}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(FX_CACHE_FILE)
        log.info("FX-курсы обновлены: {} валют", len(rates))
        return rates

    if FX_CACHE_FILE.exists():                       # фетч не удался -> старый кеш (даже протухший)
        try:
            old: dict[str, float] | None = json.loads(
                FX_CACHE_FILE.read_text(encoding="utf-8")).get("rates")
            if old:
                log.warning("FX: API недоступен — использую старый кеш")
                return old
        except (json.JSONDecodeError, OSError):
            pass
    log.warning("FX: ни API, ни кеша — хардкод-фолбэк")
    return dict(_FALLBACK)
