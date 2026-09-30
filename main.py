#!/usr/bin/env python3
"""
Plan zajęć grupy „I Technik masażysta_we” (TEB Edukacja Poznań) jako strona WWW.

Co godzinę (GitHub Actions):
  1. Chromium (Playwright) otwiera Strefę Słuchacza i czeka, aż Cloudflare przepuści,
  2. zbiera linki do PDF-ów i pobiera je w tej samej sesji przeglądarki,
  3. z tabel w PDF wyciąga wiersz grupy: godziny, przedmiot, prowadzący, sala,
  4. pobiera plan semestralny z EduPage (sp-teb-poznan.edupage.org): dokleja nazwy przedmiotów
     do bloków z planu sal i pokazuje zjazdy, dla których szkoła nie wrzuciła jeszcze PDF-a,
  5. zapisuje index.html, który workflow publikuje na GitHub Pages.

Gdy strona szkoły nie odpowiada, skrypt kończy się kodem 1 – workflow niczego
nie wdraża i na stronie zostaje ostatni poprawny plan.

Test lokalny bez przeglądarki:  python main.py --pdf plan1.pdf plan2.pdf
  … z zapisaną odpowiedzią EduPage:  python main.py --pdf plan.pdf --edupage-json regulartt.json
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import traceback
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

import pdfplumber
import requests
from bs4 import BeautifulSoup

# ═══════════════════════════ KONFIGURACJA ═══════════════════════════

URL_STRONY = "https://teb.pl/oddzialy/d/poznan/strefa-sluchacza/"
PREFIKS_PDF = "/wp-content/uploads/poznan/"
NAZWA_GRUPY = "I Technik masażysta_we"

# Plan semestralny w EduPage – ten sam, który widać pod
# https://sp-teb-poznan.edupage.org/timetable/view.php?num=9&class=-18
EDUPAGE_URL = "https://sp-teb-poznan.edupage.org"
EDUPAGE_NUM = os.environ.get("EDUPAGE_NUM", "9")            # num=… z linku (zmienia się z nową wersją planu)
EDUPAGE_KLASA = os.environ.get("EDUPAGE_KLASA", "1TM_we")   # nazwa klasy w EduPage (w linku class=-18)
EDUPAGE_DNI_NAPRZOD = 35                                    # dni tylko z EduPage: tyle dni do przodu

# TYLKO „I Technik masażysta_we” (także gdy nazwa jest złamana w komórce na dwie linie).
# Nie łapie „I Technik masażysta” bez „_we”, „II Technik masażysta_we” ani „…masażysta_wd”.
GRUPA_REGEX = re.compile(r"(?<!\w)I\s+Technik\s+masażysta(?:\s*_\s*|\s+)we(?!\w)", re.IGNORECASE)

# Dowolna nazwa grupy w konwencji szkoły: „II Technik masażysta_we”, „I Technik elektroradiolog A”,
# „I Opiekun medyczny”. Służy do odsiewania: komórka z nazwą innej grupy nigdy nie trafi do planu.
_LITERY = "A-Za-ząćęłńóśźżĄĆĘŁŃÓŚŹŻ"
RE_NAZWA_GRUPY = re.compile(
    rf"[IVX]{{1,4}}\s+[A-ZĄĆĘŁŃÓŚŹŻ][{_LITERY}]+(?:\s+[{_LITERY}]+){{0,4}}(?:\s*_\s*[a-z]{{1,4}})?"
)

# Wpis z planu sal: „Tomiak D. / 27 - Piekary”, „Parnasowska S. / 01 prac. dent. A - Rynek”.
RE_WPIS_SALI = re.compile(r"(\S.*?)\s*/\s*(\S.*?)\s*-\s*([A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźż.]*)(?=\s|$)")

KATALOG = Path(__file__).resolve().parent
PLIK_WYJSCIOWY = KATALOG / "index.html"
PLIK_TERMINOW = KATALOG / "terminy.json"
STREFA = ZoneInfo("Europe/Warsaw")
USER_AGENT_WZOR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{wersja} Safari/537.36"
)

W_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

# ═══════════════════════════ POMOCNICZE ═══════════════════════════


def log(tekst: str) -> None:
    print(tekst, flush=True)


def ostrzezenie(tekst: str) -> None:
    # W GitHub Actions pojawi się jako żółta adnotacja w podsumowaniu przebiegu.
    print(f"::warning::{tekst}" if W_ACTIONS else f"UWAGA: {tekst}", flush=True)


def normalizuj(tekst: str | None) -> str:
    """Ujednolica polskie znaki (NFC), twarde spacje i białe znaki, zachowuje podział na linie."""
    if not tekst:
        return ""
    tekst = unicodedata.normalize("NFC", tekst).replace("\u00a0", " ")
    linie = (" ".join(linia.split()) for linia in tekst.splitlines())
    return "\n".join(linia for linia in linie if linia)


def jest_nazwa_grupy(tekst: str) -> bool:
    """Czy cała komórka to nazwa jakiejś grupy (a nie przedmiot, prowadzący czy sala)."""
    return bool(RE_NAZWA_GRUPY.fullmatch(" ".join(tekst.split())))


def linie_opisu(tresc: str) -> list[str]:
    """
    Tekst komórki → linie do wyświetlenia. Wpis z planu sal „Tomiak D. / 27 - Piekary”
    zamienia na [„Tomiak D.”, „sala 27, Piekary”]; inny tekst zostawia linia po linii.
    """
    plaski = " ".join(tresc.split())
    wpisy = RE_WPIS_SALI.findall(plaski)
    if wpisy and not RE_WPIS_SALI.sub("", plaski).strip():
        if len(wpisy) == 1:
            kto, sala, gdzie = wpisy[0]
            return [kto, f"sala {sala}, {gdzie}"]
        return [", ".join(k for k, _, _ in wpisy)] + [f"{k}: sala {sl}, {g}" for k, sl, g in wpisy]
    return tresc.split("\n")


def nazwa_pliku(url: str) -> str:
    return unquote(Path(urlparse(url).path).name)


e = html.escape  # skrót do escapowania treści z PDF-ów

# ═══════════════════════════ DATY ═══════════════════════════

MIESIACE = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca",
            "sierpnia", "września", "października", "listopada", "grudnia"]
MIESIACE_SKROT = ["sty", "lut", "mar", "kwi", "maj", "cze", "lip", "sie", "wrz", "paź", "lis", "gru"]
DNI = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
DNI_KIEDY = ["w poniedziałek", "we wtorek", "w środę", "w czwartek", "w piątek", "w sobotę", "w niedzielę"]


def _bez_ogonkow(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


_NUMER_MIESIACA = {}
for _i, _nazwa in enumerate(MIESIACE, 1):
    _NUMER_MIESIACA[_nazwa] = _i
    _NUMER_MIESIACA[_bez_ogonkow(_nazwa)] = _i  # nazwy plików bywają bez ogonków

RE_DATA_DMR = re.compile(r"(?<!\d)(\d{1,2})[.\-_/](\d{1,2})[.\-_/](20\d{2})(?!\d)")
RE_DATA_RMD = re.compile(r"(?<!\d)(20\d{2})[.\-_/](\d{1,2})[.\-_/](\d{1,2})(?!\d)")
RE_DATA_SLOWNIE = re.compile(
    r"(?<!\d)(\d{1,2})[\s\-_.]+("
    + "|".join(sorted(_NUMER_MIESIACA, key=len, reverse=True))
    + r")[\s\-_.,]+(20\d{2})(?!\d)"
)
RE_DATA_DM = re.compile(r"(?<![\d.])(\d{1,2})[.\-_](\d{1,2})(?![\d])")

_NUMER_DNIA = {}
for _i, _nazwa in enumerate(DNI):
    _NUMER_DNIA[_nazwa] = _i
    _NUMER_DNIA[_bez_ogonkow(_nazwa)] = _i
RE_DZIEN_TYGODNIA = re.compile(r"(?<!\w)(" + "|".join(sorted(_NUMER_DNIA, key=len, reverse=True)) + r")(?!\w)")


def dzien_tygodnia(tekst: str | None) -> int | None:
    """Pierwsza nazwa dnia tygodnia w tekście (0 = poniedziałek) albo None."""
    m = RE_DZIEN_TYGODNIA.search(unicodedata.normalize("NFC", tekst or "").lower())
    return _NUMER_DNIA[m[1]] if m else None


def data_bez_roku(tekst: str | None, dzis: date, dzien_tyg: int | None = None) -> date | None:
    """
    „25.09” (np. z nazwy pliku) → pełna data. Rok: najbliższy dzisiejszej dacie, a jeśli w PDF
    jest nazwa dnia tygodnia („piątek”), wybierany jest rok, w którym ten dzień się zgadza.
    """
    for m in RE_DATA_DM.finditer(tekst or ""):
        kandydaci = []
        for rok in (dzis.year - 1, dzis.year, dzis.year + 1):
            try:
                kandydaci.append(date(rok, int(m[2]), int(m[1])))
            except ValueError:
                pass
        if dzien_tyg is not None:
            kandydaci = [k for k in kandydaci if k.weekday() == dzien_tyg] or kandydaci
        if kandydaci:
            return min(kandydaci, key=lambda k: abs((k - dzis).days))
    return None


RE_GODZINA = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)")


def znajdz_daty(tekst: str | None) -> list[date]:
    """Wszystkie daty z tekstu w kolejności występowania (03.10.2026, 2026-10-03, 3 października 2026)."""
    tekst = unicodedata.normalize("NFC", tekst or "").lower()
    trafienia: list[tuple[int, date]] = []

    def dodaj(pozycja, rok, miesiac, dzien):
        try:
            trafienia.append((pozycja, date(int(rok), int(miesiac), int(dzien))))
        except ValueError:
            pass

    for m in RE_DATA_DMR.finditer(tekst):
        dodaj(m.start(), m[3], m[2], m[1])
    for m in RE_DATA_RMD.finditer(tekst):
        dodaj(m.start(), m[1], m[2], m[3])
    for m in RE_DATA_SLOWNIE.finditer(tekst):
        dodaj(m.start(), m[3], _NUMER_MIESIACA[m[2]], m[1])
    return [d for _, d in sorted(trafienia, key=lambda t: t[0])]


def data_terminu(tekst: str, dzis: date) -> date:
    """„03.10” → data w bieżącym roku szkolnym (sierpień–lipiec); „03.10.2026” → dokładnie."""
    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s*", str(tekst))
    if not m:
        raise ValueError(f"terminy.json: zły format daty {tekst!r} – użyj DD.MM albo DD.MM.RRRR")
    dzien, miesiac, rok = int(m[1]), int(m[2]), m[3]
    if rok is None:
        poczatek = dzis.year if dzis.month >= 8 else dzis.year - 1
        rok = poczatek if miesiac >= 8 else poczatek + 1
    return date(int(rok), miesiac, dzien)


def zakres_godzin(etykiety: list[str]) -> str:
    """Etykiety kolumn bloku → „8:00–9:35”, „lekcje 1–2” albo pusty tekst."""
    niepuste = [x for x in etykiety if x]
    if not niepuste:
        return ""
    pierwsze = RE_GODZINA.findall(niepuste[0])
    ostatnie = RE_GODZINA.findall(niepuste[-1])
    if pierwsze:
        start = f"{int(pierwsze[0][0])}:{pierwsze[0][1]}"
        if len(ostatnie) >= 2:
            return f"{start}–{int(ostatnie[-1][0])}:{ostatnie[-1][1]}"
        return start
    if all(x.isdigit() for x in niepuste):
        if len(set(niepuste)) == 1:
            return f"lekcja {niepuste[0]}"
        return f"lekcje {niepuste[0]}–{niepuste[-1]}"
    return niepuste[0] if len(niepuste) == 1 else f"{niepuste[0]}–{niepuste[-1]}"


# ═══════════════════════════ MODEL DANYCH ═══════════════════════════


@dataclass
class Blok:
    godziny: str        # „8:00–9:35” albo ""
    linie: list[str]    # [przedmiot, prowadzący, sala, …]
    lekcje: int = 1     # ile kolumn (godzin lekcyjnych) zajmuje – wysokość na stronie


@dataclass
class Dzien:
    data: str | None    # ISO „2026-10-03” albo None, gdy nie udało się ustalić
    bloki: list[Blok]
    plik_url: str
    plik_nazwa: str
    etykieta: str       # tekst linku ze strony szkoły / nazwa pliku
    zrodlo: str = "pdf"     # "pdf", "pdf+edupage" (przedmioty z EduPage) albo "edupage" (brak PDF-a)
    edupage_url: str = ""   # link do planu klasy w EduPage


# ═══════════════════════════ POBIERANIE ═══════════════════════════


def wyciagnij_linki(html_strony: str) -> list[tuple[str, str]]:
    """Linki do PDF-ów z katalogu Poznania: [(url, tekst linku)] bez duplikatów."""
    soup = BeautifulSoup(html_strony, "html.parser")
    linki: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        adres = urlparse(urljoin(URL_STRONY, a["href"].strip()))
        if (adres.netloc in ("teb.pl", "www.teb.pl")
                and adres.path.startswith(PREFIKS_PDF)
                and adres.path.lower().endswith(".pdf")):
            url = adres._replace(fragment="").geturl()
            etykieta = normalizuj(a.get_text(" ")).replace("\n", " ")
            if url not in linki or (etykieta and not linki[url]):
                linki[url] = etykieta
    return list(linki.items())


def _to_pdf(dane: bytes) -> bool:
    # Cloudflare potrafi zwrócić stronę HTML z kodem 200 – sprawdzamy nagłówek pliku.
    return b"%PDF" in dane[:1024]


def pobierz_plik(kontekst, url: str, user_agent: str) -> bytes | None:
    for proba in range(1, 3):
        try:  # 1) w sesji przeglądarki – ma ciasteczka od Cloudflare
            odp = kontekst.request.get(url, timeout=60_000)
            if odp.ok:
                dane = odp.body()
                if _to_pdf(dane):
                    return dane
        except Exception as blad:
            log(f"    przeglądarka: {blad}")
        try:  # 2) zwykłe requests – tak jak w poprzedniej wersji
            odp = requests.get(url, headers={"User-Agent": user_agent}, timeout=60)
            if odp.ok and _to_pdf(odp.content):
                return odp.content
        except requests.RequestException as blad:
            log(f"    requests: {blad}")
        time.sleep(3 * proba)
    return None


def otworz_strone(strona) -> str | None:
    selektor = f'a[href*="{PREFIKS_PDF}"]'
    for proba in range(1, 4):
        try:
            log(f"Otwieram {URL_STRONY} (próba {proba}/3)")
            strona.goto(URL_STRONY, wait_until="domcontentloaded", timeout=60_000)
            # Zamiast sztywnych 6 s: czekamy, aż Cloudflare przepuści i w DOM pojawią się linki.
            strona.wait_for_selector(selektor, state="attached", timeout=45_000)
            return strona.content()
        except Exception as blad:
            pierwsza_linia = str(blad).splitlines()[0] if str(blad) else ""
            ostrzezenie(f"Próba {proba}/3 nieudana: {type(blad).__name__}: {pierwsza_linia}")
            strona.wait_for_timeout(5_000 * proba)
    return None


def pobierz_pdfy() -> tuple[list[dict] | None, list[str]]:
    """Zwraca (lista plików, ostrzeżenia dla strony). None = strona szkoły nie odpowiedziała."""
    from playwright.sync_api import sync_playwright

    ostrzezenia: list[str] = []
    with sync_playwright() as p:
        przegladarka = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        try:
            # User-Agent zgodny z faktyczną wersją Chromium – spójniejszy dla Cloudflare.
            user_agent = USER_AGENT_WZOR.format(wersja=przegladarka.version)
            kontekst = przegladarka.new_context(
                user_agent=user_agent, locale="pl-PL", timezone_id="Europe/Warsaw",
                viewport={"width": 1366, "height": 900},
            )
            html_strony = otworz_strone(kontekst.new_page())
            if html_strony is None:
                return None, ostrzezenia

            linki = wyciagnij_linki(html_strony)
            log(f"Znalezione pliki PDF: {len(linki)}")
            pliki = []
            for url, etykieta in linki:
                nazwa = nazwa_pliku(url)
                dane = pobierz_plik(kontekst, url, user_agent)
                if dane is None:
                    ostrzezenie(f"Nie udało się pobrać {nazwa}")
                    ostrzezenia.append(f"Nie udało się pobrać pliku {nazwa}.")
                    continue
                log(f"  pobrano {nazwa} ({len(dane) // 1024} KB)")
                pliki.append({"url": url, "nazwa": nazwa, "etykieta": etykieta, "dane": dane})
            return pliki, ostrzezenia
        finally:
            przegladarka.close()


# ═══════════════════════════ ANALIZA PDF ═══════════════════════════


def _scal(wartosci, tolerancja=1.5) -> list[float]:
    wynik: list[float] = []
    for v in sorted(wartosci):
        if not wynik or v - wynik[-1] > tolerancja:
            wynik.append(v)
    return wynik


def _zloz_linie(slowa: list[dict], tolerancja=2) -> list[list[dict]]:
    """Grupuje słowa w linie (klastrowanie po osi Y – bez „przeskakiwania” na granicy zaokrąglenia)."""
    linie: list[list] = []
    for w in sorted(slowa, key=lambda w: (w["top"], w["x0"])):
        if linie and abs(w["top"] - linie[-1][0]) <= tolerancja:
            linie[-1][1].append(w)
        else:
            linie.append([w["top"], [w]])
    return [sorted(linia, key=lambda w: w["x0"]) for _, linia in linie]


def _obrocony(znak: dict) -> bool:
    """Znak obrócony o 90° (np. pionowe godziny w nagłówku planu sal)."""
    m = znak.get("matrix")
    return bool(m) and abs(m[1]) > 0.5 and abs(m[0]) < 0.5


def _slowa_obrocone(znaki: list[dict]) -> list[dict]:
    """
    Tekst pionowy → „słowa” jak dla zwykłego tekstu. Bez tego pdfplumber zwraca każdy znak
    pionowego „17:35-18:20” jako osobne słowo, w dodatku od końca („0”, „2”, „:”, „8”…).
    """
    pionowe: list[list[dict]] = []
    for z in sorted((z for z in znaki if _obrocony(z)), key=lambda z: (round(z["x0"]), z["top"])):
        if pionowe and abs(z["x0"] - pionowe[-1][0]["x0"]) <= 1.5:
            pionowe[-1].append(z)
        else:
            pionowe.append([z])
    slowa = []
    for grupa in pionowe:
        od_dolu = grupa[0]["matrix"][1] > 0          # obrót w lewo: czytamy od dołu do góry
        grupa.sort(key=lambda z: -z["top"] if od_dolu else z["top"])
        fragmenty: list[list[dict]] = []
        for z in grupa:                               # przerwa w pionie = inne słowo / inna komórka
            if fragmenty:
                p_ = fragmenty[-1][-1]
                if max(z["top"] - p_["bottom"], p_["top"] - z["bottom"]) <= 2:
                    fragmenty[-1].append(z)
                    continue
            fragmenty.append([z])
        for f in fragmenty:
            tekst = normalizuj("".join(z["text"] for z in f))
            if tekst:
                slowa.append({"text": tekst, "x0": min(z["x0"] for z in f), "x1": max(z["x1"] for z in f),
                              "top": min(z["top"] for z in f), "bottom": max(z["bottom"] for z in f)})
    return slowa


def _tekst_slow(slowa: list[dict]) -> str:
    return "\n".join(" ".join(w["text"] for w in linia) for linia in _zloz_linie(slowa))


def _bloki(pozycje: list[tuple], tekst) -> list[Blok]:
    """
    pozycje: [(id_etykiety, etykieta_godzin, klucz_komorek)] po kolei w czasie.
    Łączy sąsiednie pozycje z tą samą komórką (scalenie) albo tym samym tekstem (2 godziny
    tego samego przedmiotu). Pomija puste komórki i komórki z nazwą grupy.
    """
    bloki: list[dict] = []
    biezacy, poprzedni_klucz = None, None
    for id_etykiety, etykieta, klucz in pozycje:
        tresc = "\n".join(t for t in map(tekst, klucz) if t)
        if not tresc or jest_nazwa_grupy(tresc):
            biezacy = poprzedni_klucz = None
            continue
        if biezacy and (klucz == poprzedni_klucz or tresc == biezacy["tresc"]):
            biezacy["kolumny"].append((id_etykiety, etykieta))
        else:
            biezacy = {"tresc": tresc, "kolumny": [(id_etykiety, etykieta)]}
            bloki.append(biezacy)
        poprzedni_klucz = klucz
    wynik = []
    for b in bloki:
        kolumny = list(dict.fromkeys(b["kolumny"]))  # jedna komórka z godziną = jedna lekcja
        wynik.append(Blok(zakres_godzin([et for _, et in kolumny]), linie_opisu(b["tresc"]), len(kolumny)))
    return wynik


def grupa_z_tabeli(tabela, slowa: list[dict], pamiec: dict | None = None) -> list[tuple[str, list[Blok]]]:
    """
    Szuka grupy w tabeli z liniami siatki i zwraca [(opis trafienia, bloki)].
    Obsługuje dwa układy planu:
      • grupy w wierszach – nazwa w pierwszych kolumnach, godziny w nagłówku u góry,
      • grupy w kolumnach – nazwa w wierszu nagłówka obok innych grup, godziny w kolumnie z boku.
    Działa na geometrii komórek, więc radzi sobie z komórkami scalonymi (2–3 godziny tego
    samego przedmiotu, wspólny wykład), a tekst komórki ma wszystkie linie.
    """
    komorki = [tuple(c) for c in tabela.cells]
    if len(komorki) < 4:
        return []
    xs = _scal([c[0] for c in komorki] + [c[2] for c in komorki])
    ys = _scal([c[1] for c in komorki] + [c[3] for c in komorki])
    if len(xs) < 3 or len(ys) < 2:
        return []
    srodki_x = [(a + b) / 2 for a, b in zip(xs, xs[1:])]
    srodki_y = [(a + b) / 2 for a, b in zip(ys, ys[1:])]

    def komorka_w(x, y):
        pasujace = [c for c in komorki if c[0] - .5 <= x <= c[2] + .5 and c[1] - .5 <= y <= c[3] + .5]
        return min(pasujace, key=lambda c: (c[2] - c[0]) * (c[3] - c[1]), default=None)

    siatka = [[komorka_w(x, y) for x in srodki_x] for y in srodki_y]
    liczba_kolumn, liczba_wierszy = len(srodki_x), len(srodki_y)
    teksty: dict = {}

    def tekst(c) -> str:
        if c is None:
            return ""
        if c not in teksty:
            teksty[c] = _tekst_slow([
                w for w in slowa
                if c[0] <= (w["x0"] + w["x1"]) / 2 <= c[2] and c[1] <= (w["top"] + w["bottom"]) / 2 <= c[3]
            ])
        return teksty[c]

    def unikalne(komorki_wiersza):
        return [c for c in dict.fromkeys(komorki_wiersza) if c]

    # ── układ „grupy w kolumnach” ──
    def z_kolumny(kom_nazwy) -> list[Blok]:
        kolumny_grupy = [k for k, x in enumerate(srodki_x) if kom_nazwy[0] <= x <= kom_nazwy[2]]
        wiersze = [i for i, y in enumerate(srodki_y) if y > kom_nazwy[3]]
        for j, i in enumerate(wiersze):  # kolejny nagłówek z grupami = koniec tej części tabeli
            if any(jest_nazwa_grupy(tekst(c)) for c in unikalne(siatka[i])):
                wiersze = wiersze[:j]
                break
        kol_godzin, najwiecej = None, 1
        for k in range(liczba_kolumn):
            if k in kolumny_grupy:
                continue
            n = sum(1 for c in unikalne(siatka[i][k] for i in wiersze) if RE_GODZINA.search(tekst(c)))
            if n > najwiecej:
                kol_godzin, najwiecej = k, n
        pozycje = []
        for i in wiersze:
            kom_godz = siatka[i][kol_godzin] if kol_godzin is not None else None
            klucz = tuple(unikalne(siatka[i][k] for k in kolumny_grupy))
            pozycje.append((kom_godz or i, tekst(kom_godz).replace("\n", " "), klucz))
        return _bloki(pozycje, tekst)

    # ── układ „grupy w wierszach” ──
    def wiersz_naglowka() -> int | None:
        """Wiersz z największą liczbą godzin (8:00-8:45); zapasowo – numery lekcji 1, 2, 3…"""
        najlepszy, indeks = 0, None
        for r in range(liczba_wierszy):
            n = sum(1 for c in unikalne(siatka[r]) if RE_GODZINA.search(tekst(c)))
            if n > najlepszy:
                najlepszy, indeks = n, r
        if najlepszy >= 2:
            return indeks
        for r in range(liczba_wierszy):
            if sum(1 for c in unikalne(siatka[r]) if tekst(c).isdigit()) >= 3:
                return r
        return None

    # Tabela na kolejnej stronie PDF zwykle nie ma własnego nagłówka z godzinami –
    # wtedy bierzemy godziny zapamiętane z poprzedniej tabeli (dopasowanie po położeniu kolumn).
    naglowek = wiersz_naglowka()
    pamiec = pamiec if pamiec is not None else {}
    if naglowek is not None:
        pamiec["kolumny"] = [(srodki_x[k], siatka[naglowek][k], tekst(siatka[naglowek][k]).replace("\n", " "))
                             for k in range(liczba_kolumn)]
    zapamietane = pamiec.get("kolumny", []) if naglowek is None else []

    def naglowek_kolumny(k):
        """(identyfikator komórki nagłówka, etykieta godzin) dla kolumny k."""
        if naglowek is not None:
            c = siatka[naglowek][k]
            return (c or k), tekst(c).replace("\n", " ")
        if zapamietane:
            x, c, etykieta = min(zapamietane, key=lambda z: abs(z[0] - srodki_x[k]))
            if abs(x - srodki_x[k]) <= 4:
                return (c or ("pamięć", x)), etykieta
        return k, ""

    def z_wiersza(wiersz, kom_nazwy, koniec) -> list[Blok]:
        # Pasmo = wiersze siatki zajmowane przez komórkę z nazwą (grupa może mieć „podwiersze”).
        pasmo = [i for i, y in enumerate(srodki_y) if kom_nazwy[1] <= y <= kom_nazwy[3]]
        pozycje = []
        for k in range(koniec + 1, liczba_kolumn):
            id_nagl, etykieta = naglowek_kolumny(k)
            if etykieta and not RE_GODZINA.search(etykieta) and not etykieta.isdigit():
                continue  # kolumny typu „Uwagi”, „Suma godzin”
            pozycje.append((id_nagl, etykieta, tuple(unikalne(siatka[i][k] for i in pasmo))))
        return _bloki(pozycje, tekst)

    wyniki: list[tuple[str, list[Blok]]] = []
    uzyte = set()
    for wiersz in siatka:
        w_wierszu = unikalne(wiersz)

        # Układ „grupy w kolumnach”: nasza nazwa stoi w wierszu obok nazw innych grup.
        nasza = next((c for c in w_wierszu if GRUPA_REGEX.search(tekst(c))), None)
        if nasza and any(c != nasza and jest_nazwa_grupy(tekst(c)) for c in w_wierszu):
            if nasza not in uzyte:
                uzyte.add(nasza)
                wyniki.append((f"{' '.join(tekst(nasza).split())} (grupy w kolumnach)", z_kolumny(nasza)))
            continue

        # Układ „grupy w wierszach”: nazwa może być rozbita na pierwsze komórki („I” | „Technik masażysta_we”).
        kom_nazwy, koniec, zebrane, widziane = None, -1, "", set()
        for k in range(min(4, liczba_kolumn - 1)):
            c = wiersz[k]
            if c is None or c in widziane:
                continue
            widziane.add(c)
            zebrane = f"{zebrane} {tekst(c)}"
            if GRUPA_REGEX.search(zebrane):
                kom_nazwy, koniec = c, k
                break
        if kom_nazwy is None or kom_nazwy in uzyte:
            continue
        uzyte.add(kom_nazwy)
        while koniec + 1 < liczba_kolumn and wiersz[koniec + 1] == kom_nazwy:
            koniec += 1
        wyniki.append((f"{' '.join(zebrane.split())} (grupy w wierszach)", z_wiersza(wiersz, kom_nazwy, koniec)))
    return wyniki


def grupa_z_tekstu(slowa: list[dict]) -> list[tuple[str, list[Blok]]]:
    """Metoda zapasowa dla PDF-ów bez linii tabeli: linie tekstu + kolumny po odstępach."""

    def kolumny(linia, przerwa=15):
        wynik: list[list[dict]] = []
        for w in linia:
            if wynik and w["x0"] - wynik[-1][-1]["x1"] <= przerwa:
                wynik[-1].append(w)
            else:
                wynik.append([w])
        return wynik

    def tekst(kol):
        return " ".join(w["text"] for w in kol)

    def srodek(obj):
        return (obj[0]["x0"] + obj[-1]["x1"]) / 2 if isinstance(obj, list) else (obj["x0"] + obj["x1"]) / 2

    linie = _zloz_linie(slowa, tolerancja=4)
    naglowek = max(linie, key=lambda l: sum(bool(RE_GODZINA.search(w["text"])) for w in l), default=[])
    kol_nagl = [k for k in kolumny(naglowek, przerwa=4) if RE_GODZINA.search(tekst(k))]
    if len(kol_nagl) < 2:
        kol_nagl = []
    srodki_nagl = [srodek(k) for k in kol_nagl]
    etykiety_nagl = [tekst(k) for k in kol_nagl]

    wyniki = []
    for nr, linia in enumerate(linie):
        kol_linii = kolumny(linia)
        nasza = next((i for i, k in enumerate(kol_linii) if GRUPA_REGEX.search(tekst(k))), None)
        if nasza is None:
            continue

        if any(i != nasza and jest_nazwa_grupy(tekst(k)) for i, k in enumerate(kol_linii)):
            # Układ „grupy w kolumnach”: zbieramy tekst pod naszą nazwą aż do kolejnego nagłówka.
            lewa = (kol_linii[nasza - 1][-1]["x1"] + kol_linii[nasza][0]["x0"]) / 2 if nasza > 0 else float("-inf")
            prawa = ((kol_linii[nasza][-1]["x1"] + kol_linii[nasza + 1][0]["x0"]) / 2
                     if nasza + 1 < len(kol_linii) else float("inf"))
            bloki: list[dict] = []
            poprzednia_pusta = True
            for kolejna in linie[nr + 1:]:
                kol_kolejnej = kolumny(kolejna)
                if any(jest_nazwa_grupy(tekst(k)) for k in kol_kolejnej):
                    break
                tresc = " ".join(w["text"] for w in kolejna if lewa <= srodek(w) <= prawa)
                pierwsza = tekst(kol_kolejnej[0]) if kol_kolejnej else ""
                etykieta = pierwsza if RE_GODZINA.search(pierwsza) and not (lewa <= srodek(kol_kolejnej[0]) <= prawa) else ""
                if not tresc:
                    poprzednia_pusta = True
                    continue
                if bloki and not poprzednia_pusta and (not etykieta or bloki[-1]["linie"][0] == tresc):
                    if etykieta:            # ta sama lekcja w kolejnej godzinie
                        bloki[-1]["etykiety"].append(etykieta)
                    elif tresc not in bloki[-1]["linie"]:  # kolejna linia tej samej komórki
                        bloki[-1]["linie"].append(tresc)
                else:
                    bloki.append({"linie": [tresc], "etykiety": [etykieta]})
                poprzednia_pusta = False
            wyniki.append((f"{tekst(kol_linii[nasza])} (grupy w kolumnach, bez linii tabeli)",
                           [Blok(zakres_godzin(b["etykiety"]), b["linie"], max(1, len([x for x in b["etykiety"] if x])))
                            for b in bloki]))
            continue

        # Układ „grupy w wierszach”. Zamiast jednej linii tekstu bierzemy całe PASMO wiersza
        # (od połowy odległości do grupy wyżej do połowy odległości do grupy niżej), więc wpisy
        # złamane na dwie linie („… / 01 prac. dent. A -” ↵ „Rynek”) nie giną.
        nazwa = kol_linii[nasza]
        y_nazwy = (nazwa[0]["top"] + nazwa[0]["bottom"]) / 2
        # Nazwy grup stoją w jednej kolumnie → wiersze to linie zaczynające się w tym samym miejscu.
        y_wierszy = sorted({round((l[0]["top"] + l[0]["bottom"]) / 2, 1) for l in linie
                            if abs(l[0]["x0"] - nazwa[0]["x0"]) <= 4})
        wyzej = [y for y in y_wierszy if y < y_nazwy - 1]
        nizej = [y for y in y_wierszy if y > y_nazwy + 1]
        gora = (wyzej[-1] + y_nazwy) / 2 if wyzej else y_nazwy - 12
        dol = (nizej[0] + y_nazwy) / 2 if nizej else y_nazwy + 12
        prawa_nazwy = max(w["x1"] for w in nazwa)
        w_pasmie = [w for w in slowa
                    if gora <= (w["top"] + w["bottom"]) / 2 <= dol and w["x0"] > prawa_nazwy + 2]

        # Komórki: fragmenty linii złączone w pionie, jeśli nachodzą na siebie w poziomie.
        fragmenty = [f for l in _zloz_linie(w_pasmie) for f in kolumny(l, przerwa=6)]
        komorki_tekstu: list[dict] = []
        for f in sorted(fragmenty, key=lambda f: f[0]["x0"]):
            x0, x1 = f[0]["x0"], f[-1]["x1"]
            for k in komorki_tekstu:
                if x0 < k["x1"] and x1 > k["x0"]:
                    k["slowa"] += f
                    k["x0"], k["x1"] = min(k["x0"], x0), max(k["x1"], x1)
                    break
            else:
                komorki_tekstu.append({"x0": x0, "x1": x1, "slowa": list(f)})

        bloki: list[dict] = []
        for k in sorted(komorki_tekstu, key=lambda k: k["x0"]):
            tresc = GRUPA_REGEX.sub("", _tekst_slow(k["slowa"])).strip(" |_\n")
            if not tresc or jest_nazwa_grupy(tresc):
                continue
            indeksy: list[int] = []
            if kol_nagl:
                indeksy = [i for i, c in enumerate(srodki_nagl) if k["x0"] - 4 <= c <= k["x1"] + 4]
                if not indeksy:
                    indeksy = [min(range(len(srodki_nagl)), key=lambda i: abs(srodki_nagl[i] - (k["x0"] + k["x1"]) / 2))]
            poprzedni = bloki[-1] if bloki else None
            # Ten sam wpis w sąsiednich godzinach → jeden blok
            if poprzedni and indeksy and poprzedni["indeksy"] and poprzedni["tresc"] == tresc \
                    and indeksy[0] == poprzedni["indeksy"][-1] + 1:
                poprzedni["indeksy"] += indeksy
            else:
                bloki.append({"tresc": tresc, "indeksy": indeksy})
        wyniki.append((f"{tekst(nazwa)} (grupy w wierszach, bez linii tabeli)",
                       [Blok(zakres_godzin([etykiety_nagl[i] for i in b["indeksy"]]), linie_opisu(b["tresc"]),
                             max(1, len(b["indeksy"]))) for b in bloki]))
    return wyniki


def data_nad_tabela(strona, tabela) -> date | None:
    """Ostatnia data w tekście nad tabelą – zwykle tytuł „Plan zajęć – sobota 03.10.2026”."""
    x0, gora, x1, _ = strona.bbox
    gora_tabeli = tabela.bbox[1]
    if gora_tabeli - gora < 5:
        return None
    try:
        daty = znajdz_daty(strona.crop((x0, gora, x1, gora_tabeli)).extract_text())
    except ValueError:
        return None
    return daty[-1] if daty else None


def analizuj_pdf(plik: dict) -> list[Dzien]:
    ostatnia_data: date | None = None
    dni: list[Dzien] = []
    dzis = datetime.now(STREFA).date()
    pamiec_naglowka: dict = {}  # godziny z nagłówka – dla tabel na kolejnych stronach

    with pdfplumber.open(io.BytesIO(plik["dane"])) as pdf:
        # Data pliku: pełna data z tekstu linku / nazwy pliku, a jeśli jest bez roku
        # („plan-salek-25.09.pdf”) – rok dobrany do dnia tygodnia wpisanego w PDF („piątek”).
        dzien_tyg = dzien_tygodnia(pdf.pages[0].extract_text() if pdf.pages else "")
        data_pliku = (next(iter(znajdz_daty(plik["etykieta"]) + znajdz_daty(plik["nazwa"])), None)
                      or data_bez_roku(plik["etykieta"], dzis, dzien_tyg)
                      or data_bez_roku(Path(plik["nazwa"]).stem, dzis, dzien_tyg))
        for nr_strony, strona in enumerate(pdf.pages, 1):
            # y_tolerance=1: domyślne 3 pt „łańcuchowo” skleja linie komórek wyśrodkowanych w pionie
            # z dwuliniowymi wpisami obok – litery z dwóch linii mieszały się („Parna s o w s k a”).
            poziome = strona.filter(lambda o: not (o.get("object_type") == "char" and _obrocony(o)))
            slowa = [dict(w, text=normalizuj(w["text"])) for w in poziome.extract_words(y_tolerance=1)]
            slowa += _slowa_obrocone(strona.chars)
            daty_strony = znajdz_daty(strona.extract_text())
            trafienia: list[tuple[date | None, str, list[Blok]]] = []

            for tabela in strona.find_tables():
                # Kolejność: tytuł nad tabelą → poprzednia tabela (ciąg dalszy na nowej stronie)
                # → tekst linku / nazwa pliku → dowolna data na stronie.
                data = (data_nad_tabela(strona, tabela) or ostatnia_data or data_pliku
                        or (daty_strony[0] if daty_strony else None))
                ostatnia_data = data or ostatnia_data
                trafienia += [(data, opis, bloki) for opis, bloki in grupa_z_tabeli(tabela, slowa, pamiec_naglowka)]

            if not trafienia:
                data = ((daty_strony[0] if daty_strony else None) or ostatnia_data or data_pliku)
                ostatnia_data = data or ostatnia_data
                trafienia = [(data, opis, bloki) for opis, bloki in grupa_z_tekstu(slowa)]

            for data, opis, bloki in trafienia:
                # W logu Actions widać dokładnie, który wiersz/kolumna został uznany za naszą grupę.
                log(f"    strona {nr_strony}: „{opis}”, dzień {data or '?'}, bloków: {len(bloki)}")
                dni.append(Dzien(
                    data=data.isoformat() if data else None, bloki=bloki,
                    plik_url=plik["url"], plik_nazwa=plik["nazwa"],
                    etykieta=plik["etykieta"] or plik["nazwa"],
                ))
    return dni


def uporzadkuj(dni: list[Dzien]) -> list[Dzien]:
    widziane, wynik = set(), []
    for d in dni:
        klucz = (d.data, json.dumps([asdict(b) for b in d.bloki], ensure_ascii=False))
        if klucz not in widziane:
            widziane.add(klucz)
            wynik.append(d)
    return sorted(wynik, key=lambda d: (d.data is None, d.data or "", d.plik_nazwa))


# ═══════════════════════════ EDUPAGE (plan semestralny) ═══════════════════════════
#
# Przeglądarka planu EduPage przy otwarciu wysyła JEDNO zapytanie regularttGetData i dostaje cały
# plan na semestr (wszystkie tygodnie); strzałki tylko przełączają widok lokalnie. Wysyłamy to samo.
#
# Po co: plan sal z PDF podaje prowadzącego i salę, ale nie przedmiot – EduPage go dokleja. Dni, dla
# których szkoła nie wrzuciła jeszcze PDF-a, pokazujemy z EduPage (z uwagą, że sale mogą się zmienić).
# EduPage to źródło pomocnicze: gdy nie odpowie, strona powstaje jak dotąd z samych PDF-ów.

# Zakres dat w nazwie tygodnia: „5: 28.09 - 4.10”. Zakres z rokiem („31.08.2026-31.01.2027”) nie pasuje.
RE_ZAKRES_TYGODNIA = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.?\s*[-–]\s*(\d{1,2})\.(\d{1,2})\.?(?!\d|\.\d)")
# Prowadzący w formacie planu sal: „Kobyłka U.”, „Barczak-Ciupak M.”, „Karwaszewski R.E.”
RE_NAUCZYCIEL = re.compile(r"^\s*([^\W\d_][\w\-]*)\s+(?:[^\W\d_]\.\s*)+$")
SKROTY_DNI = {"pn": 0, "pon": 0, "wt": 1, "sr": 2, "sro": 2, "cz": 3, "czw": 3, "pt": 4, "pia": 4,
              "so": 5, "sb": 5, "sob": 5, "n": 6, "nd": 6, "nie": 6}


@dataclass
class ZajeciaEdu:
    data: str               # ISO
    p1: int                 # pierwsza i ostatnia godzina lekcyjna
    p2: int
    od: int | None          # minuty od północy
    do: int | None
    przedmiot: str
    nauczyciele: list[str]
    sale: list[str]
    grupa: str


def _klucz(tekst: str | None) -> str:
    return _bez_ogonkow(normalizuj(tekst)).lower()


def _minuty(tekst) -> int | None:
    m = RE_GODZINA.search(str(tekst or ""))
    return int(m[1]) * 60 + int(m[2]) if m else None


def _hhmm(minuty: int) -> str:
    return f"{minuty // 60}:{minuty % 60:02d}"


def _wez(kontener, klucz):
    """Element po id („3”) – EduPage trzyma perioddata/daydata raz jako słownik, raz jako listę."""
    if isinstance(kontener, dict):
        return kontener.get(str(klucz))
    if isinstance(kontener, list) and str(klucz).isdigit() and int(klucz) < len(kontener):
        return kontener[int(klucz)]
    return None


def _tabele_edupage(odp) -> dict[str, list[dict]]:
    """Odpowiedź regularttGetData → {nazwa tabeli: wiersze}. Szukamy listy [{id, data_rows}, …]."""
    def szukaj(o, glebokosc=0):
        if glebokosc > 6:
            return None
        if isinstance(o, list) and o and all(isinstance(t, dict) and isinstance(t.get("id"), str)
                                             and isinstance(t.get("data_rows"), list) for t in o):
            return o
        dzieci = o.values() if isinstance(o, dict) else o if isinstance(o, list) else []
        for dziecko in dzieci:
            if isinstance(dziecko, (dict, list)) and (wynik := szukaj(dziecko, glebokosc + 1)) is not None:
                return wynik
        return None

    tabele = szukaj(odp)
    if tabele is None:
        klucze = list(odp)[:10] if isinstance(odp, dict) else type(odp).__name__
        raise ValueError(f"w odpowiedzi nie ma tabel planu (klucze: {klucze})")
    return {t["id"]: t["data_rows"] for t in tabele}


def pobierz_edupage() -> dict:
    """To samo zapytanie, które wysyła przeglądarka planu EduPage przy otwarciu strony."""
    sesja = requests.Session()
    sesja.headers["User-Agent"] = USER_AGENT_WZOR.format(wersja="140.0.0.0")
    strona = sesja.get(f"{EDUPAGE_URL}/timetable/view.php?num={EDUPAGE_NUM}", timeout=30)  # ciasteczka sesji
    m = re.search(r"gsechash[\"']?\s*[:=]\s*[\"']([0-9a-f]+)[\"']", strona.text)
    odp = sesja.post(
        f"{EDUPAGE_URL}/timetable/server/regulartt.js?__func=regularttGetData",
        json={"__args": [None, EDUPAGE_NUM], "__gsh": m[1] if m else "00000000"},
        timeout=60,
    )
    odp.raise_for_status()
    return odp.json()


def _nazwa(wiersz: dict | None) -> str:
    return normalizuj((wiersz or {}).get("name") or (wiersz or {}).get("short") or "")


def _nauczyciel(wiersz: dict | None) -> str:
    """Jak w planie sal: „Kobyłka U.”; gdy EduPage ukrywa nazwiska – to, co udostępnia (np. skrót)."""
    nazwisko = normalizuj((wiersz or {}).get("lastname"))
    imie = normalizuj((wiersz or {}).get("firstname"))
    if nazwisko:
        return f"{nazwisko} {imie[0]}." if imie else nazwisko
    return _nazwa(wiersz)


def _dzien_tygodnia_edu(wiersz: dict) -> int | None:
    for tekst in (wiersz.get("name"), wiersz.get("short")):
        if (nr := dzien_tygodnia(tekst)) is not None:
            return nr
        skrot = _klucz(tekst).rstrip(".")
        if skrot in SKROTY_DNI:
            return SKROTY_DNI[skrot]
    return None


def _czas(wg_id: dict, okres: int, dzien_id: str, dzwonki) -> tuple[int | None, int | None]:
    """Godziny lekcji jak w wydruku EduPage: okres → dzwonki klasy/lekcji → godziny w danym dniu."""
    wiersz = wg_id.get("periods", {}).get(str(okres))
    if not wiersz:
        return None, None
    od, do = wiersz.get("starttime"), wiersz.get("endtime")
    wg_dzwonkow = None
    if dzwonki and str(dzwonki) != "0":
        wg_dzwonkow = _wez((wg_id.get("bells", {}).get(str(dzwonki)) or {}).get("perioddata"), okres)
        if wg_dzwonkow:
            od, do = wg_dzwonkow.get("starttime") or od, wg_dzwonkow.get("endtime") or do
    w_dniu = _wez((wg_dzwonkow or {}).get("daydata") or wiersz.get("daydata"), dzien_id)
    if w_dniu:
        od, do = w_dniu.get("starttime") or od, w_dniu.get("endtime") or do
    return _minuty(od), _minuty(do)


def zajecia_edupage(odp: dict, dzis: date) -> tuple[list[ZajeciaEdu], str]:
    """Wszystkie zajęcia klasy EDUPAGE_KLASA z planu semestralnego: każdy tydzień, każdy dzień."""
    T = _tabele_edupage(odp)
    wg_id = {nazwa: {str(w.get("id")): w for w in wiersze} for nazwa, wiersze in T.items()}
    szukana = _klucz(EDUPAGE_KLASA)
    klasa = next((k for k in T.get("classes", [])
                  if szukana in (_klucz(k.get("name")), _klucz(k.get("short")), _klucz(str(k.get("id"))))), None)
    if klasa is None:
        raise ValueError(f"nie ma klasy {EDUPAGE_KLASA!r} w planie num={EDUPAGE_NUM} "
                         f"(są: {', '.join(_nazwa(k) for k in T.get('classes', []))})")
    klasa_id = str(klasa["id"])
    link = f"{EDUPAGE_URL}/timetable/view.php?num={EDUPAGE_NUM}&class={klasa_id}"

    # Tygodnie: numer (pozycja w masce „weeks” karty) → data początku z nazwy „5: 28.09 - 4.10”
    tygodnie: list[tuple[int, date]] = []
    for i, w in enumerate(T.get("weeks", [])):
        if m := RE_ZAKRES_TYGODNIA.search(_nazwa(w)):
            nr = int(w["id"]) if str(w.get("id", "")).isdigit() else i
            tygodnie.append((nr, data_terminu(f"{m[1]}.{m[2]}", dzis)))
    if not tygodnie:
        raise ValueError("nazwy tygodni nie zawierają dat: " + ", ".join(_nazwa(w) for w in T.get("weeks", [])))

    dni = T.get("days", [])
    dni_tygodnia = [_dzien_tygodnia_edu(d) for d in dni]
    if None in dni_tygodnia:
        ostrzezenie("EduPage: nie rozpoznano dnia tygodnia dla: "
                    + ", ".join(_nazwa(d) for d, n in zip(dni, dni_tygodnia) if n is None))
    if len(T.get("terms", [])) > 1:
        ostrzezenie("EduPage: plan ma kilka okresów (terms) – uwzględniam zajęcia ze wszystkich.")

    wynik: list[ZajeciaEdu] = []
    for karta in T.get("cards", []):
        lekcja = wg_id.get("lessons", {}).get(str(karta.get("lessonid")))
        if not lekcja or not karta.get("days") or not str(karta.get("period", "")).isdigit():
            continue  # karta nieumieszczona w planie
        if klasa_id not in [str(c) for c in lekcja.get("classids") or []]:
            continue
        p1 = int(karta["period"])
        p2 = p1 + int(lekcja.get("durationperiods") or 1) - 1
        dzwonki = lekcja.get("bell") or klasa.get("bell")
        przedmiot = _nazwa(wg_id.get("subjects", {}).get(str(lekcja.get("subjectid")))) or "zajęcia"
        nauczyciele = [n for n in (_nauczyciel(wg_id.get("teachers", {}).get(str(t)))
                                   for t in lekcja.get("teacherids") or []) if n]
        sale = [s for s in (_nazwa(wg_id.get("classrooms", {}).get(str(c)))
                            for c in karta.get("classroomids") or [] if c) if s]
        grupa = ", ".join(g for g in lekcja.get("groupnames") or [] if g)
        maska_tygodni = karta.get("weeks") or ""
        for di, znak in enumerate(karta["days"]):
            if znak != "1" or di >= len(dni) or dni_tygodnia[di] is None:
                continue
            dzien_id = str(dni[di].get("id", di))
            od, _ = _czas(wg_id, p1, dzien_id, dzwonki)
            _, do = _czas(wg_id, p2, dzien_id, dzwonki)
            for nr, poczatek in tygodnie:
                if nr < len(maska_tygodni) and maska_tygodni[nr] == "0":
                    continue  # ten sam filtr co w przeglądarce planu: weeks[tydzień] != "0"
                dt = poczatek + timedelta(days=(dni_tygodnia[di] - poczatek.weekday()) % 7)
                wynik.append(ZajeciaEdu(dt.isoformat(), p1, p2, od, do, przedmiot, nauczyciele, sale, grupa))

    log(f"EduPage: klasa {_nazwa(klasa)} (id {klasa_id}), tygodni z datami: {len(tygodnie)}, zajęć: {len(wynik)}")
    return wynik, link


def bloki_edupage(zajecia: list[ZajeciaEdu]) -> list[Blok]:
    """Zajęcia jednego dnia → bloki jak z PDF; kolejne godziny tych samych zajęć łączymy w jeden blok."""
    scalone: list[ZajeciaEdu] = []
    for z in sorted(zajecia, key=lambda z: (z.p1, z.grupa, z.przedmiot)):
        klucz = (z.przedmiot, z.nauczyciele, z.sale, z.grupa)
        poprzednie = next((s for s in reversed(scalone) if (s.przedmiot, s.nauczyciele, s.sale, s.grupa) == klucz), None)
        if poprzednie and z.p1 == poprzednie.p2 + 1:
            poprzednie.p2, poprzednie.do = z.p2, z.do
        else:
            scalone.append(replace(z))

    bloki = []
    for z in scalone:
        if z.od is not None and z.do is not None:
            godziny = f"{_hhmm(z.od)}–{_hhmm(z.do)}"
        else:
            godziny = f"lekcje {z.p1}–{z.p2}" if z.p2 > z.p1 else f"lekcja {z.p1}"
        szczegoly = [x for x in (", ".join(z.nauczyciele), ", ".join(z.sale), z.grupa) if x]
        bloki.append(Blok(godziny, [z.przedmiot] + szczegoly, z.p2 - z.p1 + 1))
    return bloki


def _zakres_minut(godziny: str) -> tuple[int | None, int | None]:
    czasy = [int(g) * 60 + int(m) for g, m in RE_GODZINA.findall(godziny or "")]
    return (czasy[0], czasy[-1]) if len(czasy) >= 2 else (None, None)


def wzbogac_dzien(dzien: Dzien, zajecia: list[ZajeciaEdu], link: str) -> Dzien:
    """Blok z planu sal (prowadzący + sala) dostaje nazwę przedmiotu z EduPage."""
    nowe = []
    for b in dzien.bloki:
        od, do = _zakres_minut(b.godziny)
        m = RE_NAUCZYCIEL.match(b.linie[0]) if b.linie else None
        nazwisko = _klucz(m[1]) if m else ""

        def prowadzi(z: ZajeciaEdu) -> bool:
            return any(nazwisko in _klucz(n) for n in z.nauczyciele)

        pasujace = [z for z in zajecia
                    if od is not None and z.od is not None and z.do is not None and z.od < do and od < z.do]
        if nazwisko:
            # Równoległe zajęcia innej grupy odpadają, jeśli prowadzący się zgadza.
            pasujace = [z for z in pasujace if prowadzi(z)] or pasujace or [z for z in zajecia if prowadzi(z)]
        if not pasujace:
            nowe.append(b)
            continue

        przedmioty = list(dict.fromkeys(z.przedmiot for z in sorted(pasujace, key=lambda z: z.p1)))
        linie = [" / ".join(przedmioty)] + b.linie
        wg_planu = list(dict.fromkeys(n for z in pasujace for n in z.nauczyciele))
        # Inny prowadzący niż w planie semestralnym = możliwe zastępstwo. Tylko gdy EduPage podaje nazwiska.
        if nazwisko and any(RE_NAUCZYCIEL.match(n) for n in wg_planu) and not any(prowadzi(z) for z in pasujace):
            linie.append("w planie EduPage: " + ", ".join(wg_planu))
        nowe.append(Blok(b.godziny, linie, b.lekcje))
    return replace(dzien, bloki=nowe, zrodlo="pdf+edupage", edupage_url=link)


def polacz_z_edupage(dni: list[Dzien], zajecia: list[ZajeciaEdu], link: str, dzis: date) -> list[Dzien]:
    """PDF ma pierwszeństwo (aktualne sale), EduPage dokleja przedmioty i dni bez PDF-a."""
    if not zajecia:
        return dni
    wg_daty: dict[str, list[ZajeciaEdu]] = {}
    for z in zajecia:
        wg_daty.setdefault(z.data, []).append(z)

    wynik = [wzbogac_dzien(d, wg_daty[d.data], link) if d.data in wg_daty else d for d in dni]
    daty_pdf = {d.data for d in dni}
    koniec = dzis + timedelta(days=EDUPAGE_DNI_NAPRZOD)
    dodane = 0
    for iso in sorted(wg_daty):
        if iso not in daty_pdf and dzis <= date.fromisoformat(iso) <= koniec:
            wynik.append(Dzien(data=iso, bloki=bloki_edupage(wg_daty[iso]), plik_url=link, plik_nazwa="EduPage",
                               etykieta="Plan semestralny w EduPage", zrodlo="edupage", edupage_url=link))
            dodane += 1
    uzupelnione = sum(d.zrodlo == "pdf+edupage" for d in wynik)
    log(f"EduPage: dni z PDF uzupełnione o przedmioty: {uzupelnione}, dni tylko z EduPage: {dodane}")
    return uporzadkuj(wynik)


def wczytaj_edupage(argumenty, dzis: date) -> tuple[list[ZajeciaEdu], str]:
    """EduPage jest dodatkiem – błąd tylko logujemy, a strona powstaje z samych PDF-ów."""
    if argumenty.bez_edupage:
        return [], ""
    try:
        if argumenty.edupage_json:
            odp = json.loads(Path(argumenty.edupage_json).read_text(encoding="utf-8-sig"))
        else:
            odp = pobierz_edupage()
        return zajecia_edupage(odp, dzis)
    except Exception as blad:
        ostrzezenie(f"EduPage pominięte – {type(blad).__name__}: {blad}")
        return [], ""


# ═══════════════════════════ TERMINY (ręczne) ═══════════════════════════

TYPY_TERMINOW = {"zaliczenie": "zaliczenie", "test": "test", "zajecia": "zajęcia", "info": "informacja"}
PRIORYTET = {"zaliczenie": 0, "test": 1, "zajecia": 2, "info": 3}


def wczytaj_terminy(dzis: date) -> list[dict]:
    # Na serwerach GitHuba (Linux) „Terminy.json” ≠ „terminy.json” – szukamy bez względu na wielkość liter.
    plik = next((x for x in KATALOG.iterdir() if x.is_file() and x.name.lower() == PLIK_TERMINOW.name), None)
    if plik is None:
        ostrzezenie(f"Brak pliku {PLIK_TERMINOW.name} obok {Path(__file__).name} – sekcja terminów będzie pusta.")
        return []
    wynik = []
    for poz in json.loads(plik.read_text(encoding="utf-8-sig")):
        typ = poz.get("typ", "info")
        if typ not in TYPY_TERMINOW:
            raise ValueError(f"terminy.json: nieznany typ {typ!r} – dozwolone: {', '.join(TYPY_TERMINOW)}")
        wynik.append({"data": data_terminu(poz["data"], dzis).isoformat(), "typ": typ,
                      "opis": str(poz["opis"]).strip()})
    log(f"Terminy: wczytano {len(wynik)} z pliku {plik.name}")
    return sorted(wynik, key=lambda t: t["data"])  # sortowanie stabilne: kolejność w dniu zostaje


# ═══════════════════════════ HTML ═══════════════════════════

CSS = """
:root{
  --tlo:#F1F6FC;--papier:#FFFFFF;--tusz:#0E2A47;--szary:#51708F;--linia:#D5E3F1;
  --blekit:#1463BF;--blekit-mocny:#0F4F9E;--blekit-tlo:#E7F0FB;--blekit-jasny:#C9DDF5;
  --czerwien:#C4283A;--czerwien-tlo:#FCE8EA;
  --bursztyn:#955A00;--bursztyn-tlo:#FFF1D9;
  --info-tlo:#ECF1F7;
  --pasek:#1463BF;--pasek-drugi:#D3E4FA;
  color-scheme:light dark;
}
@media (prefers-color-scheme:dark){
  :root{
    --tlo:#0B1B2E;--papier:#10243B;--tusz:#E6EFF9;--szary:#93AECB;--linia:#22405F;
    --blekit:#62A8F2;--blekit-mocny:#93C4F8;--blekit-tlo:#15314F;--blekit-jasny:#24476E;
    --czerwien:#F28B97;--czerwien-tlo:#3B1E28;
    --bursztyn:#F0B85A;--bursztyn-tlo:#382C16;
    --info-tlo:#162C44;
    --pasek:#0F3A68;--pasek-drugi:#A9C9EE;
  }
}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--tlo);color:var(--tusz);
  font-family:"Bricolage Grotesque","Segoe UI Variable","Segoe UI",system-ui,-apple-system,Roboto,"Helvetica Neue",Arial,sans-serif;
  font-size:1.0625rem;line-height:1.5;font-optical-sizing:auto}
a{color:var(--blekit);text-underline-offset:.18em}
a:focus-visible,summary:focus-visible{outline:3px solid var(--blekit);outline-offset:3px;border-radius:4px}

/* Niebieski pasek u góry – jak oznakowanie w przychodni */
.pasek{background:var(--pasek);color:#fff}
.pasek-srodek{max-width:44rem;margin:0 auto;padding:2.1rem 1.25rem 2.3rem;display:flex;align-items:center;gap:1rem}
.krzyz{flex:none;width:clamp(3rem,13vw,4.4rem);height:auto}
.krzyz .tlo-krzyza{fill:#fff}
.krzyz .ramie{fill:var(--pasek)}
h1{margin:0;font-size:clamp(2.6rem,11.5vw,4.4rem);line-height:.92;font-weight:800;font-stretch:75%;letter-spacing:-.02em}
.grupa{margin:.55rem 0 0;font-size:1.05rem;line-height:1.35;color:var(--pasek-drugi)}
.grupa strong{color:#fff;font-weight:650}

.strona{max-width:44rem;margin:0 auto;padding:1.5rem 1.25rem 4rem}
.gora{display:grid;gap:1rem}
.baner{padding:1rem 1.15rem 1.1rem;border-radius:16px;background:var(--blekit-tlo);border:1px solid var(--blekit-jasny)}
.baner-tytul{margin:0 0 .6rem;font-weight:700;color:var(--blekit-mocny)}
.baner .wpisy{gap:.4rem}
.ostrzezenie{padding:.85rem 1.1rem;border-radius:14px;background:var(--bursztyn-tlo);font-size:.95rem}
.ostrzezenie p{margin:0}
.ostrzezenie ul{margin:.4rem 0 0;padding-left:1.2rem}

.dni{margin-top:1.5rem;display:grid;gap:1rem}
.dzien{background:var(--papier);border:1px solid var(--linia);border-radius:18px;padding:1.3rem 1.15rem 1.35rem}
.dzien.jest-najblizszy{border:2px solid var(--blekit);box-shadow:0 10px 28px -16px rgba(20,99,191,.55)}
.dzien-naglowek{display:flex;align-items:flex-end;justify-content:space-between;gap:.5rem 1rem;flex-wrap:wrap;margin-bottom:1.1rem}
.dzien-naglowek h2{margin:0;font-size:clamp(1.9rem,7vw,2.6rem);line-height:1;font-weight:750;font-stretch:78%;letter-spacing:-.01em}
.dzien-tygodnia{display:block;margin-bottom:.3rem;font-size:1rem;font-weight:600;font-stretch:100%;letter-spacing:0;color:var(--blekit)}
.kiedy{font-weight:650;color:var(--szary);white-space:nowrap}
.kiedy:empty{display:none}
.jest-najblizszy .dzien-naglowek .kiedy{padding:.2rem .7rem;border-radius:999px;background:var(--blekit);color:var(--papier);font-size:.9rem}

.zajecia{list-style:none;margin:0;padding:0;display:grid;gap:.45rem}
.blok{display:grid;grid-template-columns:4.1rem 1fr;gap:0 .8rem;min-height:calc(var(--lekcje,1) * 2.9rem)}
.zajecia.bez-godzin .blok{grid-template-columns:1fr}
.zajecia.bez-godzin .blok-godz{display:none}
.blok-godz{display:flex;flex-direction:column;align-items:flex-end;padding-top:.55rem;font-variant-numeric:tabular-nums;line-height:1.25;text-align:right}
.blok-godz .od{font-size:1rem;font-weight:700}
.blok-godz .do{font-size:.85rem;color:var(--szary)}
.blok-godz .inne{font-size:.85rem;font-weight:600;color:var(--szary)}
.blok-tresc{display:flex;flex-direction:column;justify-content:flex-start;padding:.55rem .9rem .6rem;border-radius:12px;background:var(--blekit-tlo);border-left:4px solid var(--blekit)}
.przedmiot{font-weight:650;line-height:1.3}
.szczegol{font-size:.9rem;line-height:1.35;color:var(--szary)}
.wolne{margin:0;padding:.9rem 1rem;border-radius:12px;background:var(--info-tlo);color:var(--szary)}
.zrodlo{margin:1rem 0 0;font-size:.85rem;color:var(--szary)}
.dzien.z-edupage,.dzien.z-edupage.jest-najblizszy{border-style:dashed}
.z-edupage .blok-tresc{border-left-style:dashed}

.brak{margin:0;padding:1.1rem 1.15rem;border-radius:16px;background:var(--papier);border:1px dashed var(--blekit-jasny);color:var(--szary)}

.minione{margin-top:1.5rem}
.minione summary{cursor:pointer;font-weight:650;color:var(--blekit);padding:.4rem 0}
.minione .dzien{margin-top:.75rem;opacity:.6}

.terminy{margin-top:2.5rem;background:var(--papier);border:1px solid var(--linia);border-radius:18px;padding:1.4rem 1.15rem .5rem}
.terminy h2{margin:0;font-size:clamp(1.6rem,6vw,2rem);line-height:1.05;font-weight:750;font-stretch:80%}
.terminy-opis{margin:.4rem 0 1rem;color:var(--szary);font-size:.95rem}
.lista-terminow{list-style:none;margin:0;padding:0}
.termin{display:grid;grid-template-columns:5.25rem 1fr;gap:1rem;padding:.95rem 0;border-top:1px solid var(--linia)}
.termin-data{display:flex;flex-direction:column;line-height:1.15}
.termin-data time{font-size:1.35rem;font-weight:750;font-stretch:82%}
.termin-data .dzien-tyg{font-size:.85rem;color:var(--szary)}
.termin-data .kiedy{margin-top:.25rem;font-size:.85rem}
.termin.jest-nastepny time,.termin.jest-nastepny .kiedy{color:var(--blekit)}
.termin.minal{opacity:.5}
.wpisy{list-style:none;margin:0;padding:0;display:grid;gap:.55rem}
.wpis{line-height:1.45}
.wpis .typ{display:inline-block;margin-right:.45rem;vertical-align:.08em}
.typ{font-size:.8rem;font-weight:650;padding:.12rem .55rem;border-radius:999px;white-space:nowrap}
.typ-zaliczenie{background:var(--czerwien-tlo);color:var(--czerwien)}
.typ-test{background:var(--bursztyn-tlo);color:var(--bursztyn)}
.typ-zajecia{background:var(--blekit-tlo);color:var(--blekit-mocny)}
.typ-info{background:var(--info-tlo);color:var(--szary)}

.stopka{margin-top:2.5rem;padding-top:1.25rem;border-top:1px solid var(--linia);font-size:.875rem;color:var(--szary)}
.stopka p{margin:0 0 .5rem}

@media (max-width:26rem){
  .blok{grid-template-columns:3.7rem 1fr;gap:0 .65rem}
  .termin{grid-template-columns:4.6rem 1fr;gap:.8rem}
  .dzien,.terminy{padding-left:.95rem;padding-right:.95rem}
}
@media print{
  body{background:#fff}
  .pasek{background:#fff;color:#000}
  .grupa,.grupa strong{color:#000}
  .baner,.stopka,.minione{display:none}
  .dzien{break-inside:avoid;box-shadow:none}
}
"""

JS = """
(function () {
  function roznica(iso) {
    var p = iso.split("-");
    var cel = new Date(+p[0], +p[1] - 1, +p[2]);
    var dzis = new Date(); dzis.setHours(0, 0, 0, 0);
    return Math.round((cel - dzis) / 86400000);
  }
  function kiedy(r) {
    if (r === 0) return "dziś";
    if (r === 1) return "jutro";
    if (r > 1) return "za " + r + " dni";
    return "";
  }

  var minione = document.getElementById("minione");
  var pierwszy = null, liczbaMinionych = 0;
  document.querySelectorAll("#dni .dzien[data-data]").forEach(function (el) {
    var r = roznica(el.dataset.data);
    if (r < 0) { minione.appendChild(el); liczbaMinionych++; return; }
    el.querySelector(".kiedy").textContent = kiedy(r);
    if (!pierwszy) { pierwszy = el; el.classList.add("jest-najblizszy"); }
  });
  if (liczbaMinionych) {
    document.getElementById("liczba-minionych").textContent = liczbaMinionych;
    minione.hidden = false;
  }
  if (!document.querySelector("#dni .dzien") && !document.getElementById("pusto")) {
    document.getElementById("brak-nadchodzacych").hidden = false;
  }

  var nastepny = null;
  document.querySelectorAll(".termin[data-data]").forEach(function (el) {
    var r = roznica(el.dataset.data);
    var k = el.querySelector(".kiedy");
    if (r < 0) { el.classList.add("minal"); k.textContent = "minęło"; return; }
    k.textContent = kiedy(r);
    if (!nastepny) { nastepny = el; el.classList.add("jest-nastepny"); }
  });

  var baner = document.getElementById("baner");
  if (nastepny) {
    var tytul = document.createElement("p");
    tytul.className = "baner-tytul";
    tytul.textContent = "Najbliższy termin: " + kiedy(roznica(nastepny.dataset.data)) + ", " + nastepny.dataset.kiedy;
    baner.appendChild(tytul);
    baner.appendChild(nastepny.querySelector(".wpisy").cloneNode(true));
    baner.hidden = false;
  }
})();
"""

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48'%3E"
           "%3Crect width='48' height='48' rx='12' fill='%231463BF'/%3E"
           "%3Crect x='19' y='9' width='10' height='30' rx='2.5' fill='white'/%3E"
           "%3Crect x='9' y='19' width='30' height='10' rx='2.5' fill='white'/%3E%3C/svg%3E")

KRZYZ_SVG = ('<svg class="krzyz" viewBox="0 0 48 48" aria-hidden="true">'
             '<rect class="tlo-krzyza" width="48" height="48" rx="12"/>'
             '<rect class="ramie" x="19" y="9" width="10" height="30" rx="2.5"/>'
             '<rect class="ramie" x="9" y="19" width="30" height="10" rx="2.5"/></svg>')


def html_godzin(godziny: str) -> str:
    """„9:45–12:15” → początek nad końcem; inne etykiety (np. „lekcje 1–2”) bez zmian."""
    czesci = godziny.split("–")
    if len(czesci) == 2 and all(RE_GODZINA.fullmatch(c) for c in czesci):
        return f'<span class="od">{czesci[0]}</span><span class="do">{czesci[1]}</span>'
    if RE_GODZINA.fullmatch(godziny):
        return f'<span class="od">{godziny}</span>'
    return f'<span class="inne">{e(godziny)}</span>' if godziny else ""


def html_dnia(d: Dzien) -> str:
    if d.data:
        dt = date.fromisoformat(d.data)
        tytul = (f'<span class="dzien-tygodnia">{DNI[dt.weekday()]}</span>'
                 f'<time datetime="{d.data}">{dt.day} {MIESIACE[dt.month - 1]}</time>')
        atrybut = f' data-data="{d.data}"'
    else:
        tytul = f'<span class="dzien-tygodnia">data nieustalona</span>{e(d.etykieta)}'
        atrybut = ""

    if d.bloki:
        klasa = "zajecia" if any(b.godziny for b in d.bloki) else "zajecia bez-godzin"
        pozycje = []
        for b in d.bloki:
            przedmiot, *reszta = b.linie
            szczegoly = "".join(f'<span class="szczegol">{e(linia)}</span>' for linia in reszta)
            pozycje.append(
                f'<li class="blok" style="--lekcje:{min(max(b.lekcje, 1), 6)}">'
                f'<span class="blok-godz">{html_godzin(b.godziny)}</span>'
                f'<div class="blok-tresc"><span class="przedmiot">{e(przedmiot)}</span>{szczegoly}</div></li>'
            )
        tresc = f'<ol class="{klasa}">{"".join(pozycje)}</ol>'
    else:
        tresc = '<p class="wolne">Tego dnia grupa nie ma zajęć.</p>'

    if d.zrodlo == "edupage":
        klasa_dnia = "dzien z-edupage"
        zrodlo = (f'Z planu semestralnego w <a href="{e(d.edupage_url)}" target="_blank" rel="noopener">EduPage</a>. '
                  f"Sale mogą się jeszcze zmienić – plan sal szkoła publikuje na kilka dni przed zjazdem.")
    else:
        klasa_dnia = "dzien"
        zrodlo = f'Plik PDF: <a href="{e(d.plik_url)}" target="_blank" rel="noopener">{e(d.plik_nazwa)}</a>'
        if d.zrodlo == "pdf+edupage":
            zrodlo += f' · przedmioty z <a href="{e(d.edupage_url)}" target="_blank" rel="noopener">EduPage</a>'

    return (
        f'<article class="{klasa_dnia}"{atrybut}>'
        f'<header class="dzien-naglowek"><h2>{tytul}</h2><span class="kiedy"></span></header>'
        f"{tresc}"
        f'<p class="zrodlo">{zrodlo}</p>'
        f"</article>"
    )


def html_terminow(terminy: list[dict]) -> str:
    if not terminy:
        return '<p class="brak">Nie ma jeszcze żadnych terminów. Dopisz je w pliku terminy.json w repozytorium.</p>'
    wg_dat: dict[str, list[dict]] = {}
    for t in terminy:
        wg_dat.setdefault(t["data"], []).append(t)

    pozycje = []
    for iso, wpisy in wg_dat.items():
        dt = date.fromisoformat(iso)
        wpisy = sorted(wpisy, key=lambda w: PRIORYTET[w["typ"]])
        lista = "".join(
            f'<li class="wpis"><span class="typ typ-{w["typ"]}">{TYPY_TERMINOW[w["typ"]]}</span>'
            f'<span class="opis">{e(w["opis"])}</span></li>'
            for w in wpisy
        )
        kiedy = f"{DNI_KIEDY[dt.weekday()]} {dt.day} {MIESIACE[dt.month - 1]}"
        pozycje.append(
            f'<li class="termin" data-data="{iso}" data-kiedy="{e(kiedy)}">'
            f'<div class="termin-data"><time datetime="{iso}">{dt.day} {MIESIACE_SKROT[dt.month - 1]}</time>'
            f'<span class="dzien-tyg">{DNI[dt.weekday()]}</span><span class="kiedy"></span></div>'
            f'<ul class="wpisy">{lista}</ul></li>'
        )
    return f'<ol class="lista-terminow">{"".join(pozycje)}</ol>'


def renderuj(dni: list[Dzien], terminy: list[dict], ostrzezenia: list[str],
             odcisk: str, zaktualizowano: datetime) -> str:
    if dni:
        html_dni = "".join(html_dnia(d) for d in dni)
    else:
        html_dni = (f'<p class="brak" id="pusto">W planach opublikowanych w Strefie Słuchacza nie ma jeszcze '
                    f'wiersza grupy {e(NAZWA_GRUPY)}. Zajęcia pojawią się tu same, gdy szkoła doda nowy plik.</p>')

    html_ostrz = ""
    if ostrzezenia:
        lista = "".join(f"<li>{e(o)}</li>" for o in ostrzezenia)
        html_ostrz = (f'<div class="ostrzezenie" role="status"><p>Część planu może być niepełna. '
                      f'Brakujące dni sprawdź w <a href="{URL_STRONY}">Strefie Słuchacza</a>.</p>'
                      f"<ul>{lista}</ul></div>")

    kiedy = (f"{zaktualizowano.day} {MIESIACE[zaktualizowano.month - 1]} {zaktualizowano.year}, "
             f"{zaktualizowano:%H:%M}")
    opis = f"Aktualny plan zajęć grupy {NAZWA_GRUPY} w TEB Edukacja Poznań oraz terminy zaliczeń."

    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="odcisk-danych" content="{odcisk}">
<meta name="description" content="{e(opis)}">
<meta property="og:title" content="Plan zajęć: {e(NAZWA_GRUPY)}">
<meta property="og:description" content="{e(opis)}">
<meta name="theme-color" content="#1463BF" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0F3A68" media="(prefers-color-scheme: dark)">
<title>Plan zajęć: {e(NAZWA_GRUPY)}</title>
<link rel="icon" href="{FAVICON}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wdth,wght@12..96,75..100,200..800&display=swap">
<style>{CSS}</style>
</head>
<body>
<header class="pasek">
  <div class="pasek-srodek">
    {KRZYZ_SVG}
    <div>
      <h1>Plan zajęć</h1>
      <p class="grupa">Grupa <strong>{e(NAZWA_GRUPY)}</strong>, TEB Edukacja Poznań</p>
    </div>
  </div>
</header>
<main class="strona">
  <div class="gora">
    <div class="baner" id="baner" hidden></div>
    {html_ostrz}
  </div>

  <section class="dni" id="dni" aria-label="Dni zajęć">
    {html_dni}
  </section>
  <p class="brak" id="brak-nadchodzacych" hidden>Szkoła nie opublikowała jeszcze planu na kolejny zjazd. Pojawi się tu sam, gdy tylko w Strefie Słuchacza będzie nowy plik PDF.</p>
  <details class="minione" id="minione" hidden><summary>Minione dni (<span id="liczba-minionych">0</span>)</summary></details>

  <section class="terminy" aria-labelledby="terminy-tytul">
    <h2 id="terminy-tytul">Zaliczenia i ważne terminy</h2>
    <p class="terminy-opis">Terminy spisane przez grupę na zajęciach.</p>
    {html_terminow(terminy)}
  </section>

  <footer class="stopka">
    <p>Plan pochodzi z plików PDF w <a href="{URL_STRONY}">Strefie Słuchacza TEB Poznań</a> oraz z <a href="{EDUPAGE_URL}/timetable/view.php?num={EDUPAGE_NUM}">planu semestralnego w EduPage</a> (nazwy przedmiotów i zjazdy bez PDF-a). Strona sprawdza je co godzinę, ostatnia aktualizacja: {kiedy}.</p>
    <p><a href="https://github.com/KrySQL/TM_WE/">Repozytorium</a></p>
  </footer>
</main>
<script>{JS}</script>
</body>
</html>
"""


def zapisz_strone(dni: list[Dzien], terminy: list[dict], ostrzezenia: list[str]) -> bool:
    """Zapisuje index.html tylko wtedy, gdy zmieniły się dane albo sam skrypt."""
    dane = json.dumps({"dni": [asdict(d) for d in dni], "terminy": terminy, "ostrzezenia": ostrzezenia},
                      ensure_ascii=False, sort_keys=True).encode()
    odcisk = hashlib.sha256(dane + Path(__file__).read_bytes()).hexdigest()[:16]

    if PLIK_WYJSCIOWY.exists():
        m = re.search(r'<meta name="odcisk-danych" content="([0-9a-f]+)"',
                      PLIK_WYJSCIOWY.read_text(encoding="utf-8"))
        if m and m.group(1) == odcisk:
            log("Brak zmian – index.html zostaje bez zmian.")
            return False

    PLIK_WYJSCIOWY.write_text(renderuj(dni, terminy, ostrzezenia, odcisk, datetime.now(STREFA)),
                              encoding="utf-8")
    log(f"Zapisano {PLIK_WYJSCIOWY.name} (dni: {len(dni)}, terminy: {len(terminy)}).")
    return True


# ═══════════════════════════ START ═══════════════════════════


def main() -> int:
    parser = argparse.ArgumentParser(description="Generator strony z planem zajęć.")
    parser.add_argument("--pdf", nargs="+", metavar="PLIK", help="przeanalizuj lokalne PDF-y zamiast pobierać")
    parser.add_argument("--edupage-json", metavar="PLIK",
                        help="odpowiedź regularttGetData zapisana z DevTools zamiast pobierania z EduPage")
    parser.add_argument("--bez-edupage", action="store_true", help="nie łącz planu z EduPage")
    argumenty = parser.parse_args()

    dzis = datetime.now(STREFA).date()
    terminy = wczytaj_terminy(dzis)

    if argumenty.pdf:
        pliki = [{"url": Path(p).resolve().as_uri(), "nazwa": Path(p).name, "etykieta": "",
                  "dane": Path(p).read_bytes()} for p in argumenty.pdf]
        ostrzezenia: list[str] = []
    else:
        pliki, ostrzezenia = pobierz_pdfy()
        if pliki is None:
            ostrzezenie("Strona szkoły nie odpowiedziała – zostawiam poprzednią wersję index.html.")
            return 1
        if not pliki:
            ostrzezenie("Nie pobrano żadnego PDF-a – zostawiam poprzednią wersję index.html.")
            return 1

    dni: list[Dzien] = []
    for plik in pliki:
        try:
            znalezione = analizuj_pdf(plik)
            log(f"{plik['nazwa']}: wierszy grupy {len(znalezione)}")
            dni += znalezione
        except Exception:
            traceback.print_exc()
            ostrzezenie(f"Nie udało się odczytać {plik['nazwa']}")
            ostrzezenia.append(f"Nie udało się odczytać pliku {plik['nazwa']}.")

    zajecia, link_edupage = wczytaj_edupage(argumenty, dzis)
    zapisz_strone(polacz_z_edupage(uporzadkuj(dni), zajecia, link_edupage, dzis), terminy, ostrzezenia)
    return 0


if __name__ == "__main__":
    sys.exit(main())
