import os
import re
import tempfile
import urllib.parse

import pdfplumber
import requests
from bs4 import BeautifulSoup


# ============================================================
# KONFIGURACJA
# ============================================================

URL_STRONY = "https://teb.pl/oddzialy/d/poznan/strefa-sluchacza/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    )
}

SZUKANA_GRUPA = "I Technik masażysta_we"

# Tolerancja przy grupowaniu słów w jeden wiersz PDF
TOLERANCJA_Y = 5


# ============================================================
# POBIERANIE LINKÓW DO WSZYSTKICH PDF
# ============================================================

def pobierz_wszystkie_linki():
    print("=" * 70)
    print("POBIERANIE LISTY DOKUMENTÓW")
    print("=" * 70)

    print(f"\nPobieranie strony:")
    print(URL_STRONY)

    try:
        response = requests.get(
            URL_STRONY,
            headers=HEADERS,
            timeout=20
        )

        response.raise_for_status()

    except requests.RequestException as e:
        print(f"\nBłąd podczas pobierania strony:")
        print(e)
        return []

    print(f"Status HTTP: {response.status_code}")

    soup = BeautifulSoup(response.text, "html.parser")

    # --------------------------------------------------------
    # Szukamy sekcji:
    #
    # section.files
    #
    # a następnie pierwszego <ul> znajdującego się w tej sekcji
    # --------------------------------------------------------

    sekcja_files = soup.select_one("section.files")

    if not sekcja_files:
        print("\nBŁĄD:")
        print("Nie znaleziono elementu <section class='files'>.")
        return []

    lista = sekcja_files.select_one("ul")

    if not lista:
        print("\nBŁĄD:")
        print("Nie znaleziono elementu <ul> w section.files.")
        return []

    # Wszystkie linki <a> znajdujące się w <li>
    elementy_a = lista.select("li a[href]")

    print(f"\nZnaleziono elementów <a>: {len(elementy_a)}")

    linki = []

    # --------------------------------------------------------
    # Przetwarzamy każdy link
    # --------------------------------------------------------

    for numer, a in enumerate(elementy_a, start=1):

        href = a.get("href")

        if not href:
            continue

        # Zamieniamy np.
        #
        # /uploads/plik.pdf
        #
        # na:
        #
        # https://teb.pl/uploads/plik.pdf
        #
        pelny_url = urllib.parse.urljoin(
            URL_STRONY,
            href
        )

        # Pobieramy samą ścieżkę URL.
        # Dzięki temu poprawnie obsłużymy również np.
        #
        # plik.pdf?download=1
        #
        sciezka = urllib.parse.urlparse(
            pelny_url
        ).path

        # Interesują nas tylko PDF-y
        if not sciezka.lower().endswith(".pdf"):
            continue

        # Nazwa widoczna przy linku
        nazwa_pliku = a.get_text(
            separator=" ",
            strip=True
        )

        if not nazwa_pliku:
            nazwa_pliku = os.path.basename(
                sciezka
            )

        linki.append({
            "nazwa": nazwa_pliku,
            "url": pelny_url
        })

        print(f"\n[{len(linki)}] {nazwa_pliku}")
        print(f"    {pelny_url}")

    print("\n" + "-" * 70)
    print(
        f"Łącznie znaleziono {len(linki)} dokumentów PDF."
    )
    print("-" * 70)

    return linki


# ============================================================
# NORMALIZACJA TEKSTU
# ============================================================

def normalizuj_tekst(tekst):
    """
    Ujednolica tekst, np.:
    'MASAŻYSTA' -> 'masazysta'

    Dzięki temu skrypt znajdzie również tekst
    z polskimi znakami zapisany w różny sposób.
    """

    if not tekst:
        return ""

    tekst = tekst.lower()

    zamiany = {
        "ą": "a",
        "ć": "c",
        "ę": "e",
        "ł": "l",
        "ń": "n",
        "ó": "o",
        "ś": "s",
        "ź": "z",
        "ż": "z"
    }

    for znak, zamiennik in zamiany.items():
        tekst = tekst.replace(
            znak,
            zamiennik
        )

    # Usuwamy wielokrotne spacje
    tekst = re.sub(
        r"\s+",
        " ",
        tekst
    )

    return tekst.strip()


# ============================================================
# SPRAWDZENIE CZY WIERSZ DOTYCZY MASAŻYSTY
# ============================================================

def czy_wiersz_pasuje(tekst):
    """
    Sprawdza, czy dany wiersz PDF zawiera słowo
    masażysta / masazysta.
    """

    tekst_normalny = normalizuj_tekst(
        tekst
    )

    return "masazysta" in tekst_normalny


# ============================================================
# USUWANIE NAZWY GRUPY Z WIERSZA
# ============================================================

def oczysc_wiersz(tekst):
    """
    Usuwa nazwę kierunku/grupy z odnalezionego wiersza,
    aby w HTML zostały przede wszystkim godziny, sala itd.
    """

    wynik = tekst

    frazy_do_usuniecia = [
        "I Technik masażysta_we",
        "I Technik masażysta",
        "i technik masażysta_we",
        "i technik masażysta",
        "technik masażysta_we",
        "technik masażysta",
        "Technik masażysta",
        "technik masazysta",
        "Technik masazysta",
        "masażysta",
        "masazysta"
    ]

    for fraza in frazy_do_usuniecia:
        wynik = wynik.replace(
            fraza,
            ""
        )

    wynik = re.sub(
        r"\s+",
        " ",
        wynik
    )

    return wynik.strip()


# ============================================================
# ANALIZA JEDNEGO PDF
# ============================================================

def analizuj_pdf(sciezka_pdf):

    wyniki_pdf = []

    try:

        with pdfplumber.open(
            sciezka_pdf
        ) as pdf:

            print(
                f"  Liczba stron PDF: {len(pdf.pages)}"
            )

            # ------------------------------------------------
            # Przechodzimy przez wszystkie strony
            # ------------------------------------------------

            for nr_strony, strona in enumerate(
                pdf.pages,
                start=1
            ):

                print(
                    f"  Analizuję stronę {nr_strony}..."
                )

                try:
                    slowa = strona.extract_words()

                except Exception as e:
                    print(
                        f"    Błąd odczytu strony: {e}"
                    )
                    continue

                if not slowa:
                    continue

                # ------------------------------------------------
                # Grupowanie słów według wysokości Y.
                #
                # Dzięki temu słowa znajdujące się w jednym
                # wierszu tabeli trafiają razem.
                # ------------------------------------------------

                rzedy = {}

                for slowo in slowa:

                    y = round(
                        slowo["top"] / TOLERANCJA_Y
                    ) * TOLERANCJA_Y

                    if y not in rzedy:
                        rzedy[y] = []

                    rzedy[y].append(slowo)

                posortowane_wysokosci = sorted(
                    rzedy.keys()
                )

                aktualne_naglowki = []

                # ------------------------------------------------
                # Analizujemy każdy wiersz
                # ------------------------------------------------

                for y in posortowane_wysokosci:

                    slowa_wiersza = rzedy[y]

                    # Sortowanie słów od lewej do prawej
                    slowa_wiersza = sorted(
                        slowa_wiersza,
                        key=lambda x: x["x0"]
                    )

                    tekst_wiersza = " ".join(
                        slowo["text"]
                        for slowo in slowa_wiersza
                    )

                    tekst_lower = tekst_wiersza.lower()

                    # ------------------------------------------------
                    # Wykrywanie nagłówków tabeli
                    # ------------------------------------------------

                    if (
                        "semestr" in tekst_lower
                        or "8:00" in tekst_lower
                        or "godz" in tekst_lower
                        or "godz." in tekst_lower
                    ):
                        aktualne_naglowki = [
                            slowo["text"]
                            for slowo in slowa_wiersza
                        ]

                    # ------------------------------------------------
                    # Sprawdzenie czy wiersz dotyczy masażysty
                    # ------------------------------------------------

                    if czy_wiersz_pasuje(
                        tekst_wiersza
                    ):

                        print(
                            f"    ✓ Znaleziono na stronie "
                            f"{nr_strony}: "
                            f"{tekst_wiersza[:120]}"
                        )

                        zajecia_tekst = oczysc_wiersz(
                            tekst_wiersza
                        )

                        if not zajecia_tekst:
                            zajecia_tekst = tekst_wiersza

                        wyniki_pdf.append({

                            "strona": nr_strony,

                            "naglowki": (
                                " ".join(
                                    aktualne_naglowki
                                )
                                if aktualne_naglowki
                                else
                                "Brak wykrytych nagłówków"
                            ),

                            "zajecia": zajecia_tekst
                        })

    except Exception as e:

        print(
            f"  Błąd odczytu pliku PDF: {e}"
        )

    return wyniki_pdf


# ============================================================
# BEZPIECZNA NAZWA PLIKU
# ============================================================

def bezpieczna_nazwa_pliku(nazwa):

    nazwa = re.sub(
        r"[^a-zA-Z0-9ąćęłńóśźżĄĆĘŁŃÓŚŹŻ._-]",
        "_",
        nazwa
    )

    return nazwa[:100]


# ============================================================
# POBIERANIE PDF
# ============================================================

def pobierz_pdf(url):

    try:

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=30
        )

        response.raise_for_status()

        # Sprawdzamy czy serwer faktycznie zwrócił coś
        if not response.content:
            print(
                "  PDF jest pusty."
            )
            return None

        # Tworzymy plik tymczasowy
        plik_temp = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".pdf"
        )

        plik_temp.write(
            response.content
        )

        plik_temp.close()

        return plik_temp.name

    except requests.RequestException as e:

        print(
            f"  Błąd pobierania PDF: {e}"
        )

        return None

    except Exception as e:

        print(
            f"  Nieoczekiwany błąd: {e}"
        )

        return None


# ============================================================
# ESCAPOWANIE TEKSTU DO HTML
# ============================================================

def escape_html(tekst):

    if tekst is None:
        return ""

    return (
        str(tekst)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# ============================================================
# GENEROWANIE STRONY HTML
# ============================================================

def generuj_strone():

    pliki_do_sprawdzenia = (
        pobierz_wszystkie_linki()
    )

    raport_przegladu = []

    if not pliki_do_sprawdzenia:

        print(
            "\nNie znaleziono żadnych PDF-ów."
        )

        return

    # --------------------------------------------------------
    # Analiza wszystkich PDF
    # --------------------------------------------------------

    for i, plik in enumerate(
        pliki_do_sprawdzenia,
        start=1
    ):

        print("\n" + "=" * 70)

        print(
            f"[{i}/{len(pliki_do_sprawdzenia)}]"
        )

        print(
            f"Plik: {plik['nazwa']}"
        )

        print(
            f"URL: {plik['url']}"
        )

        print("=" * 70)

        status_sukces = False
        wyniki = []

        sciezka_temp = None

        try:

            # Pobieramy PDF
            sciezka_temp = pobierz_pdf(
                plik["url"]
            )

            if sciezka_temp:

                print(
                    "Rozpoczynam analizę PDF..."
                )

                wyniki = analizuj_pdf(
                    sciezka_temp
                )

                status_sukces = True

                print(
                    f"Zakończono analizę. "
                    f"Znaleziono trafień: "
                    f"{len(wyniki)}"
                )

        except Exception as e:

            print(
                f"Błąd podczas analizy: {e}"
            )

        finally:

            # Usuwamy plik tymczasowy
            if (
                sciezka_temp
                and os.path.exists(
                    sciezka_temp
                )
            ):

                try:
                    os.remove(
                        sciezka_temp
                    )

                except Exception:
                    pass

        raport_przegladu.append({

            "nazwa": plik["nazwa"],

            "url": plik["url"],

            "sukces": status_sukces,

            "dane": wyniki
        })

    # ========================================================
    # GENEROWANIE HTML
    # ========================================================

    print("\n" + "=" * 70)
    print("TWORZENIE index.html")
    print("=" * 70)

    html = f"""<!DOCTYPE html>
<html lang="pl">

<head>

    <meta charset="UTF-8">

    <meta name="viewport"
          content="width=device-width, initial-scale=1.0">

    <title>
        Plan Zajęć - {escape_html(SZUKANA_GRUPA)}
    </title>

    <style>

        * {{
            box-sizing: border-box;
        }}

        body {{
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f4f4f9;
            color: #333;
        }}

        .container {{
            max-width: 1100px;
            margin: 0 auto;
        }}

        h1 {{
            text-align: center;
            color: #2c3e50;
            margin-bottom: 10px;
        }}

        p.sub {{
            text-align: center;
            color: #7f8c8d;
            font-size: 0.9em;
            margin-bottom: 30px;
        }}

        .podsumowanie {{
            background: white;
            padding: 15px 20px;
            border-radius: 8px;
            margin-bottom: 25px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.08);
            text-align: center;
        }}

        .sekcja-pliku {{
            margin-top: 25px;
            padding: 20px;
            background: #fff;
            border-radius: 8px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            border-top: 5px solid #3498db;
        }}

        .tytul-pliku {{
            font-size: 1.15em;
            font-weight: bold;
            margin-bottom: 8px;
            color: #2c3e50;
        }}

        .url-info {{
            font-size: 0.8em;
            color: #95a5a6;
            margin-bottom: 15px;
            word-break: break-all;
        }}

        .url-info a {{
            color: #2980b9;
            text-decoration: none;
        }}

        .url-info a:hover {{
            text-decoration: underline;
        }}

        .karta {{
            background-color: #f8f9fa;
            border: 1px solid #e9ecef;
            border-radius: 5px;
            padding: 15px;
            margin-bottom: 10px;
        }}

        .naglowek {{
            font-size: 0.85em;
            color: #7f8c8d;
            margin-bottom: 8px;
            font-weight: bold;
        }}

        .zajecia {{
            font-size: 1.05em;
            color: #2c3e50;
            font-weight: bold;
        }}

        .brak-danych {{
            color: #e67e22;
            font-size: 0.9em;
            font-style: italic;
        }}

        .blad {{
            color: #c0392b;
            font-size: 0.9em;
        }}

        footer {{
            margin-top: 40px;
            padding: 20px;
            text-align: center;
            color: #95a5a6;
            font-size: 0.8em;
        }}

        @media (max-width: 600px) {{

            body {{
                padding: 10px;
            }}

            .sekcja-pliku {{
                padding: 15px;
            }}

            h1 {{
                font-size: 1.5em;
            }}

        }}

    </style>

</head>

<body>

<div class="container">

    <h1>
        Plan Zajęć: {escape_html(SZUKANA_GRUPA)}
    </h1>

    <p class="sub">
        Automatyczny monitoring ze strefy słuchacza TEB Poznań.
    </p>
"""

    # --------------------------------------------------------
    # Podsumowanie
    # --------------------------------------------------------

    liczba_plikow = len(
        raport_przegladu
    )

    liczba_trafien = sum(
        len(poz["dane"])
        for poz in raport_przegladu
    )

    html += f"""
    <div class="podsumowanie">

        Przeanalizowano:
        <strong>{liczba_plikow}</strong>
        dokumentów PDF.

        Znaleziono:
        <strong>{liczba_trafien}</strong>
        trafień dla grupy
        <strong>{escape_html(SZUKANA_GRUPA)}</strong>.

    </div>
"""

    # --------------------------------------------------------
    # Wyniki każdego PDF
    # --------------------------------------------------------

    for poz in raport_przegladu:

        html += f"""
    <div class="sekcja-pliku">

        <div class="tytul-pliku">
            📄 {escape_html(poz["nazwa"])}
        </div>

        <div class="url-info">
            Link źródłowy:
            <a href="{escape_html(poz["url"])}"
               target="_blank"
               rel="noopener noreferrer">
                {escape_html(poz["url"])}
            </a>
        </div>
"""

        # ----------------------------------------------------
        # PDF zawiera wyniki
        # ----------------------------------------------------

        if poz["dane"]:

            for wynik in poz["dane"]:

                html += f"""
        <div class="karta">

            <div class="naglowek">

                Strona {wynik["strona"]}

                <br>

                Wykryte nagłówki:
                {escape_html(wynik["naglowki"])}

            </div>

            <div class="zajecia">

                Zajęcia / Sala:

                <br>

                {escape_html(wynik["zajecia"])}

            </div>

        </div>
"""

        # ----------------------------------------------------
        # Brak wyników
        # ----------------------------------------------------

        else:

            if poz["sukces"]:

                html += """
        <div class="karta brak-danych">

            Brak zajęć dla tej grupy w tym dokumencie.

        </div>
"""

            else:

                html += """
        <div class="karta blad">

            Nie udało się pobrać lub przeanalizować
            tego dokumentu PDF.

        </div>
"""

        html += """
    </div>
"""

    # --------------------------------------------------------
    # Stopka
    # --------------------------------------------------------

    html += f"""

    <footer>

        Wygenerowano automatycznie.
        Źródło:
        TEB Edukacja Poznań.

    </footer>

</div>

</body>

</html>
"""

    # --------------------------------------------------------
    # Zapis pliku
    # --------------------------------------------------------

    try:

        with open(
            "index.html",
            "w",
            encoding="utf-8"
        ) as f:

            f.write(html)

        print(
            "\nGotowe!"
        )

        print(
            "Utworzono plik: index.html"
        )

    except Exception as e:

        print(
            f"\nBłąd zapisu index.html: {e}"
        )


# ============================================================
# START PROGRAMU
# ============================================================

if __name__ == "__main__":

    generuj_strone()
