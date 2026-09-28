#!/usr/bin/env python3
"""
Plan zajęć grupy „I Technik masażysta_we” (TEB Edukacja Poznań) jako strona WWW. 

Co godzinę (GitHub Actions):
  1. Chromium (Playwright) otwiera Strefę Słuchacza i czeka, aż Cloudflare przepuści,
  2. zbiera linki do PDF-ów i pobiera je w tej samej sesji przeglądarki,
  3. z tabel w PDF wyciąga wiersz grupy: godziny, przedmiot, prowadzący, sala,
  4. zapisuje index.html, który workflow publikuje na GitHub Pages.

Gdy strona szkoły nie odpowiada, skrypt kończy się kodem 1 – workflow niczego
nie wdraża i na stronie zostaje ostatni poprawny plan.

Test lokalny bez przeglądarki:  python main.py --pdf plan1.pdf plan2.pdf
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
from dataclasses import asdict, dataclass
from datetime import date, datetime
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

# TYLKO „I Technik masażysta_we” (także gdy nazwa jest złamana w komórce na dwie linie).
# Nie łapie „I Technik masażysta” bez „_we”, „II Technik masażysta_we” ani „…masażysta_wd”.
GRUPA_REGEX = re.compile(r"(?<!\w)I\s+Technik\s+masażysta(?:\s*_\s*|\s+)we(?!\w)", re.IGNORECASE)

# Dowolna nazwa grupy w konwencji szkoły („II Technik masażysta_we”, „I Opiekun medyczny_we”).
# Służy do odsiewania: komórka z nazwą innej grupy nigdy nie trafi do planu.
RE_NAZWA_GRUPY = re.compile(r"[IVX]{1,4}\s+\w[\w .()/-]{1,80}?\s*_\s*[a-z]{1,4}", re.IGNORECASE)

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


def _zloz_linie(slowa: list[dict], tolerancja=3) -> list[list[dict]]:
    """Grupuje słowa w linie (klastrowanie po osi Y – bez „przeskakiwania” na granicy zaokrąglenia)."""
    linie: list[list] = []
    for w in sorted(slowa, key=lambda w: (w["top"], w["x0"])):
        if linie and abs(w["top"] - linie[-1][0]) <= tolerancja:
            linie[-1][1].append(w)
        else:
            linie.append([w["top"], [w]])
    return [sorted(linia, key=lambda w: w["x0"]) for _, linia in linie]


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
        wynik.append(Blok(zakres_godzin([et for _, et in kolumny]), b["tresc"].split("\n"), len(kolumny)))
    return wynik


def grupa_z_tabeli(tabela, slowa: list[dict]) -> list[tuple[str, list[Blok]]]:
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
    naglowek: int | None | bool = False  # False = jeszcze nie szukano

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

    def z_wiersza(wiersz, kom_nazwy, koniec) -> list[Blok]:
        nonlocal naglowek
        if naglowek is False:
            naglowek = wiersz_naglowka()
        # Pasmo = wiersze siatki zajmowane przez komórkę z nazwą (grupa może mieć „podwiersze”).
        pasmo = [i for i, y in enumerate(srodki_y) if kom_nazwy[1] <= y <= kom_nazwy[3]]
        pozycje = []
        for k in range(koniec + 1, liczba_kolumn):
            kom_nagl = siatka[naglowek][k] if naglowek is not None else None
            etykieta = tekst(kom_nagl).replace("\n", " ")
            if naglowek is not None and etykieta and not RE_GODZINA.search(etykieta) and not etykieta.isdigit():
                continue  # kolumny typu „Uwagi”, „Suma godzin”
            pozycje.append((kom_nagl or k, etykieta, tuple(unikalne(siatka[i][k] for i in pasmo))))
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
    kol_nagl = [k for k in kolumny(naglowek) if RE_GODZINA.search(tekst(k))]
    if len(kol_nagl) < 2:
        kol_nagl = []

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

        # Układ „grupy w wierszach”
        bloki = []
        for kol in kol_linii:
            tresc = GRUPA_REGEX.sub("", tekst(kol)).strip(" |_")
            if not tresc or jest_nazwa_grupy(tresc):
                continue
            indeks, etykieta = None, ""
            if kol_nagl:
                indeks = min(range(len(kol_nagl)), key=lambda i: abs(srodek(kol_nagl[i]) - srodek(kol)))
                etykieta = tekst(kol_nagl[indeks])
            poprzedni = bloki[-1] if bloki else None
            # Ten sam przedmiot w sąsiednich godzinach → jeden blok (np. 8:00–9:35)
            if poprzedni and indeks is not None and poprzedni["tresc"] == tresc and poprzedni["indeks"] == indeks - 1:
                poprzedni["etykiety"].append(etykieta)
                poprzedni["indeks"] = indeks
            else:
                bloki.append({"tresc": tresc, "indeks": indeks, "etykiety": [etykieta]})
        wyniki.append((f"{tekst(kol_linii[nasza])} (grupy w wierszach, bez linii tabeli)",
                       [Blok(zakres_godzin(b["etykiety"]), [b["tresc"]], len(b["etykiety"])) for b in bloki]))
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
    data_pliku = next(iter(znajdz_daty(plik["etykieta"]) + znajdz_daty(plik["nazwa"])), None)
    ostatnia_data: date | None = None
    dni: list[Dzien] = []

    with pdfplumber.open(io.BytesIO(plik["dane"])) as pdf:
        for nr_strony, strona in enumerate(pdf.pages, 1):
            slowa = [dict(w, text=normalizuj(w["text"])) for w in strona.extract_words()]
            daty_strony = znajdz_daty(strona.extract_text())
            trafienia: list[tuple[date | None, str, list[Blok]]] = []

            for tabela in strona.find_tables():
                # Kolejność: tytuł nad tabelą → poprzednia tabela (ciąg dalszy na nowej stronie)
                # → tekst linku / nazwa pliku → dowolna data na stronie.
                data = (data_nad_tabela(strona, tabela) or ostatnia_data or data_pliku
                        or (daty_strony[0] if daty_strony else None))
                ostatnia_data = data or ostatnia_data
                trafienia += [(data, opis, bloki) for opis, bloki in grupa_z_tabeli(tabela, slowa)]

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


# ═══════════════════════════ TERMINY (ręczne) ═══════════════════════════

TYPY_TERMINOW = {"zaliczenie": "zaliczenie", "test": "test", "zajecia": "zajęcia", "info": "informacja"}
PRIORYTET = {"zaliczenie": 0, "test": 1, "zajecia": 2, "info": 3}


def wczytaj_terminy(dzis: date) -> list[dict]:
    if not PLIK_TERMINOW.exists():
        return []
    wynik = []
    for poz in json.loads(PLIK_TERMINOW.read_text(encoding="utf-8")):
        typ = poz.get("typ", "info")
        if typ not in TYPY_TERMINOW:
            raise ValueError(f"terminy.json: nieznany typ {typ!r} – dozwolone: {', '.join(TYPY_TERMINOW)}")
        wynik.append({"data": data_terminu(poz["data"], dzis).isoformat(), "typ": typ,
                      "opis": str(poz["opis"]).strip()})
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

    return (
        f'<article class="dzien"{atrybut}>'
        f'<header class="dzien-naglowek"><h2>{tytul}</h2><span class="kiedy"></span></header>'
        f"{tresc}"
        f'<p class="zrodlo">Plik PDF: <a href="{e(d.plik_url)}" target="_blank" rel="noopener">{e(d.plik_nazwa)}</a></p>'
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
    <p>Plan pochodzi z plików PDF w <a href="{URL_STRONY}">Strefie Słuchacza TEB Poznań</a>. Strona sprawdza je co godzinę, ostatnia aktualizacja: {kiedy}.</p>
    <p>Przy rozbieżnościach obowiązuje plan opublikowany przez szkołę.</p>
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
    argumenty = parser.parse_args()

    terminy = wczytaj_terminy(datetime.now(STREFA).date())

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

    zapisz_strone(uporzadkuj(dni), terminy, ostrzezenia)
    return 0


if __name__ == "__main__":
    sys.exit(main())
