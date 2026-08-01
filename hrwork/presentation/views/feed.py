"""Лента вакансий — HTML-список с клиентской фильтрацией.

Payload разделён, чтобы не грузить 200+ МБ в браузер на старте:
  feed.html       — лёгкий каркас (фильтры) + <script src> на данные
  feed-data.js    — карточные поля (VACANCIES) БЕЗ описаний (~8% веса)
  feed-desc.js    — описания {id: html} (~92% веса), грузятся ЛЕНИВО при клике
Описания нужны только в модалке; грузим их по требованию (window.__DESC).
"""
import html
import json
import shutil
import subprocess
from html.parser import HTMLParser
from typing import Any

from jinja2 import Environment, FileSystemLoader

from hrwork.application.apply.chat import chat, chat_class
from hrwork.application.apply.forms.form_status import FormSweepStatus
from hrwork.application.apply.runtime.store import store
from hrwork.config import (
    ACCENT,
    BG,
    DATA_DIR,
    EXP_LABELS,
    FEED_OUT,
    GRID,
    LANG_KEYS,
    PAPER,
    RESUME_CORE,
    RESUME_EXP_IDS,
    ROLE_PATTERNS,
    TEMPLATE_DIR,
    TEXT,
    log,
)
from hrwork.domain.parsing import has_remote
from hrwork.infrastructure.net import rates
from hrwork.infrastructure.storage import MARK_VALUES, vacancy_repository

# ── Санитизация описаний ──────────────────────────────────────────────────────
# description_html — чужой HTML (контент работодателя со страницы HH), и это
# единственное место, где данные попадают в innerHTML модалки БЕЗ esc()
# (view.js renderDesc). Чистим один раз при сборке: allowlist структурных тегов,
# все атрибуты срезаются, script/style выбрасываются вместе с содержимым.

_ALLOWED_TAGS = {"p", "br", "ul", "ol", "li", "strong", "em", "b", "i"}
_DROP_CONTENT = {"script", "style"}


class _DescSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._open: list[str] = []   # стек открытых allowed-тегов (для авто-закрытия)
        self._skip = 0               # глубина внутри script/style

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT:
            self._skip += 1
        elif tag in _ALLOWED_TAGS:
            self._out.append(f"<{tag}>")
            if tag != "br":
                self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT:
            self._skip = max(0, self._skip - 1)
        elif tag in self._open:
            while self._open:                    # закрыть вложенные до пары
                t = self._open.pop()
                self._out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self._open:                        # добить незакрытые теги
            self._out.append(f"</{self._open.pop()}>")
        return "".join(self._out)


def sanitize_desc(src: str) -> str:
    """Чужой HTML -> безопасный: allowlist тегов, без атрибутов, текст экранирован."""
    s = _DescSanitizer()
    s.feed(src)
    s.close()
    return s.result()


def _salary_fields(sal: Any) -> dict[str, Any]:
    """Зарплатные поля карточки (JS-имена sal_from/to/mid — забота презентации, не домена).
    None-вилка -> пустые значения; убирает 4× повтор `v.salary.X if v.salary else …` (Demeter)."""
    if not sal:
        return {"sal_from": None, "sal_to": None, "sal_mid": None, "currency": ""}
    return {"sal_from": sal.frm, "sal_to": sal.to, "sal_mid": sal.mid,
            "currency": sal.currency or ""}


def _last_hr_replies() -> dict[str, str]:
    """{vacancyId: ts последнего ответа РАБОТОДАТЕЛЯ, написанного человеком}.

    Сортировка «Свежие» смотрит на дату публикации вакансии, а не переписки, поэтому ответ
    HR по старой вакансии тонул в ленте и его можно было не заметить. Ботовые автоответы
    (`bot: true` — «ваш отклик зарегистрирован», ГигаРекрутёр) исключены: они приходят пачками
    сразу после отклика и вытеснили бы живые ответы наверх."""
    out: dict[str, str] = {}
    for vid, rec in store.chat_messages().items():
        ts = ""
        for m in (rec or {}).get("messages") or []:
            if not m.get("mine") and not m.get("bot") and (t := str(m.get("ts") or "")) > ts:
                ts = t
        if ts:
            out[str(vid)] = ts
    return out


def build_feed() -> None:
    vacancies = vacancy_repository().load()      # list[VacancyRecord]
    statuses = store.statuses()                  # {vacancyId: employerState} из chat_data
    forms = store.forms()                        # вакансии-опросники (нужна форма)
    # форма протухла (свип не нашёл полей / страница умерла) -> вакансия, скорее всего,
    # снята с публикации: карточка гасится серым (st-dead), как «не актуальна»
    dead_forms = {vid for vid, r in store.form_cache().items()
                  if (s := FormSweepStatus.from_code(r.get("status"))) and s.is_dead}
    hr_replies = _last_hr_replies()               # {vacancyId: ts последнего ЖИВОГО ответа}

    records = []          # лёгкие карточные поля (идут в feed-data.js)
    descs = {}            # id -> описание (идёт в feed-desc.js, грузится лениво)
    for rec in vacancies:
        v = rec.vacancy
        remote_any = v.is_remote() or has_remote(v.name + " " + rec.requirement)
        records.append({
            "id":       v.id,
            "name":     v.name,
            "url":      rec.url,
            "employer": v.employer,
            "city":     v.city,
            **_salary_fields(v.salary),
            "exp":      v.experience.label if v.experience else "",
            "schedule": v.schedule.hh_code,
            "techs":    v.techs,
            "remote_any": remote_any,
            "role":     v.role.label,
            # свежесть: возраст с создания, разрыв переоткрытия, класс, отклики
            "age":      v.age_days(),
            "gap":      v.republish_gap_days(),
            "fresh":    v.fresh_class().code,      # строковый код для JS-ленты (view.js/model.js)
            "resp":     v.responses,
            # CRM: реальный статус отклика (из chat_data) + признак формы-опросника
            "status":   statuses.get(v.id),
            "needs_form": v.id in forms,
            "form_dead": v.id in dead_forms,
            # ts последнего живого ответа HR (пусто — ответа не было): сортировка «Ответы»
            "hr_ts":    hr_replies.get(str(v.id), ""),
            "source":   v.source,          # портал-источник (агрегатор): hh / hirify / …
        })
        # описание (description_html, иначе текст requirement) — только для модалки;
        # санитизируем здесь, а не на клиенте: закрывает и serve, и file://
        descs[v.id] = sanitize_desc(rec.description_html.strip() or rec.requirement)

    cities  = sorted({r["city"] for r in records if r["city"]})
    langs   = sorted(LANG_KEYS)
    sal_max = max((r["sal_mid"] for r in records if r["sal_mid"]), default=500_000)
    # Роли-чипы: только IT-роли, что реально встретились, в порядке ROLE_PATTERNS.
    present = {r["role"] for r in records}
    roles   = [role for role in ROLE_PATTERNS if role in present]
    nonit   = sum(1 for r in records if r["role"] == "Не-IT")
    sources = sorted({r["source"] for r in records})   # порталы для чипов-фильтра

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. CSS (Jinja2 с цветовыми переменными)
    css = env.get_template("feed.css.j2").render(
        bg=BG, paper=PAPER, grid=GRID, text=TEXT, accent=ACCENT,
    )
    (DATA_DIR / "feed.css").write_text(css, encoding="utf-8")
    log.info("feed.css сохранён  ({} байт)", len(css))

    # 2. JS — собираем ES-модули (src/feed/) в один IIFE-бандл через esbuild.
    #    MVVM: model (домен) / store (ViewModel) / view (DOM) / marks (IO) / main (bootstrap).
    src_entry = TEMPLATE_DIR.parent / "src" / "feed" / "main.js"
    feed_js = DATA_DIR / "feed.js"
    esbuild = shutil.which("esbuild")
    if esbuild:
        subprocess.run(
            [esbuild, str(src_entry), "--bundle", "--format=iife",
             "--target=es2020", f"--outfile={feed_js}"],
            check=True,
        )
        log.info("feed.js собран esbuild ({:.1f} КБ)", feed_js.stat().st_size / 1024)
    else:
        # фолбэк: esbuild не установлен — берём ранее собранный бандл, если он есть
        log.warning("esbuild не найден (npm i -g esbuild) — feed.js не пересобран")

    marks = store.marks()

    # 3. feed-data.js — лёгкие карточные данные (грузится <script src>, не inline в HTML)
    fx_rates = rates.get_rates()                 # курсы per-USD (суточный кеш)
    data_js = (
        f"const VACANCIES = {json.dumps(records, ensure_ascii=False)};\n"
        f"const SAL_MAX = {sal_max};\n"
        f"const SAVED_MARKS = {json.dumps(marks, ensure_ascii=False)};\n"
        f"const FX_RATES = {json.dumps(fx_rates)};\n"                      # per-USD
        f"const FX_ALIAS = {json.dumps(rates.CURRENCY_ALIAS)};\n"          # RUR->RUB и т.п.
        # Единый источник Python->JS (иначе молча расходятся): подписи статусов и профиль резюме.
        f"const STATE_LABELS_PY = {json.dumps(chat.STATE_LABELS, ensure_ascii=False)};\n"
        f"const RESUME_CORE_PY = {json.dumps(RESUME_CORE)};\n"
        f"const RESUME_EXPS_PY = {json.dumps([EXP_LABELS[e] for e in RESUME_EXP_IDS])};\n"
        f"const MARK_VALUES_PY = {json.dumps(list(MARK_VALUES))};\n"     # словарь пометок (marks.py)
        # тупиковые виды чата (chat_class.FROZEN_KINDS): бейдж, фильтр «Личные» и счётчик
        f"const CHAT_FROZEN_PY = {json.dumps(list(chat_class.FROZEN_CODES))};\n"
    )
    (DATA_DIR / "feed-data.js").write_text(data_js, encoding="utf-8")
    log.info("feed-data.js сохранён  ({:.1f} МБ)", len(data_js.encode("utf-8")) / 1e6)

    # 4. feed-desc.js — описания (92% веса), грузятся ЛЕНИВО при первом клике по карточке
    desc_js = f"window.__DESC = {json.dumps(descs, ensure_ascii=False)};\n"
    (DATA_DIR / "feed-desc.js").write_text(desc_js, encoding="utf-8")
    log.info("feed-desc.js сохранён  ({:.1f} МБ, lazy)", len(desc_js.encode("utf-8")) / 1e6)

    # 5. HTML — лёгкий каркас: фильтры (server-side) + <script src> на данные
    n_status = sum(1 for r in records if r["status"])
    # чип «Формы»: живые+протухшие ПО ВЫДАЧЕ (не len(forms) — очередь содержит и выпавшие
    # из выдачи вакансии); формат совпадает с живым пересчётом refreshFormsChip (main.js)
    n_alive = sum(1 for r in records if r["needs_form"] and not r["form_dead"])
    n_dead = sum(1 for r in records if r["needs_form"] and r["form_dead"])
    html = env.get_template("feed.html.j2").render(
        cities=cities,
        langs=langs,
        roles=roles,
        nonit=nonit,
        sal_max=sal_max,
        total_records=len(records),
        has_status=bool(statuses) or bool(forms),
        n_forms=f"{n_alive}+{n_dead}⌛" if n_dead else str(n_alive),
        sources=sources,
    )
    FEED_OUT.write_text(html, encoding="utf-8")
    log.success("Feed -> {}  ({:.2f} МБ, {} вакансий, отметок: {}, статусов API: {}, форм: {})",
                FEED_OUT, len(html.encode("utf-8")) / 1e6, len(records), len(marks),
                n_status, len(forms))
