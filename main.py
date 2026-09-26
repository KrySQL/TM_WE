import os
import urllib.parse
from bs4 import BeautifulSoup
import pdfplumber
import requests

URL_STRONY = "https://teb.pl/oddzialy/d/poznan/strefa-sluchacza/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    )
}
SZUKANA_GRUPA = "I Technik masażysta_we"


def pobierz_wszystkie_linki():
  print(f"Pobieranie strony: {URL_STRONY} ...")
  try:
    response = requests.get(URL_STRONY, headers=HEADERS, timeout=15)
    response.raise_for_status()
  except Exception as e:
    print(f"Błąd podczas pobierania strony: {e}")
    return []

  soup = BeautifulSoup(response.text, "html.parser")
  linki = []

  # 1. Próba pobrania po wskazanym selektorze
  selektor = "body > div.root > div.page.page-departments.subpage-downloads > section.files > div > div:nth-child(1) > div > ul > li > a"
  elementy_a = soup.select(selektor)

  # 2. Selektor zapasowy, jeśli pierwszy nie zadziała
  if not elementy_a:
    print("Stosuję selektor zapasowy dla listy plików...")
    elementy_a = soup.select(
        "section.files div.container > div:nth-child(1) ul li a"
    )

  # 3. Ostatnia deska ratunku: znajdź absolutnie każdy link prowadzący do pliku .pdf na stronie
  if not elementy_a:
    print("Szukam uniwersalnie wszystkich plików PDF na stronie...")
    elementy_a = soup.find_all("a", href=True)

  seen_urls = set()
  for a in elementy_a:
    href = a.get("href")
    if href and ".pdf" in href.lower():
      pelny_url = urllib.parse.urljoin(URL_STRONY, href)
      if pelny_url not in seen_urls:
        seen_urls.add(pelny_url)
        nazwa_pliku = a.get_text(separator=" ", strip=True) or os.path.basename(
            urllib.parse.urlparse(pelny_url).path
        )
        linki.append({"nazwa": nazwa_pliku, "url": pelny_url})

  print(f"Łącznie wykryto {len(linki)} plików PDF do przeglądnięcia.")
  return linki


def analizuj_pdf(sciezka_pdf):
  wyniki_pdf = []
  TOLERANCJA_Y = 5

  try:
    with pdfplumber.open(sciezka_pdf) as pdf:
      for nr_strony, strona in enumerate(pdf.pages):
        slowa = strona.extract_words()
        if not slowa:
          continue

        rzedy = {}
        for slowo in slowa:
          y = round(slowo["top"] / TOLERANCJA_Y) * TOLERANCJA_Y
          if y not in rzedy:
            rzedy[y] = []
          rzedy[y].append(slowo["text"])

        posortowane_wysokosci = sorted(rzedy.keys())
        aktualne_naglowki = []

        for y in posortowane_wysokosci:
          wiersz = rzedy[y]
          pelny_tekst = " ".join(wiersz)
          pelny_tekst_lower = pelny_tekst.lower()

          if (
              "semestr" in pelny_tekst_lower
              or "8:00" in pelny_tekst_lower
              or "godz" in pelny_tekst_lower
          ):
            aktualne_naglowki = wiersz

          if (
              "masażysta" in pelny_tekst_lower
              or "masazysta" in pelny_tekst_lower
          ):
            print(
                f"  -> Znaleziono pasujący wiersz na stronie {nr_strony + 1}:"
                f" {pelny_tekst[:80]}..."
            )

            zajecia_tekst = pelny_tekst
            for slowo_klucz in [
                "i technik masażysta_we",
                "i technik masażysta",
                "masażysta",
                "masazysta",
            ]:
              zajecia_tekst = zajecia_tekst.replace(slowo_klucz, "").strip()

            wyniki_pdf.append({
                "strona": nr_strony + 1,
                "naglowki": (
                    " ".join(aktualne_naglowki)
                    if aktualne_naglowki
                    else "Brak wykrytych nagłówków"
                ),
                "zajecia": zajecia_tekst if zajecia_tekst else pelny_tekst,
            })
  except Exception as e:
    print(f"Błąd odczytu pliku PDF: {e}")

  return wyniki_pdf


def generuj_strone():
  pliki_do_sprawdzenia = pobierz_wszystkie_linki()
  raport_przegladu = []

  for i, plik in enumerate(pliki_do_sprawdzenia, 1):
    print(
        f"\n[{i}/{len(pliki_do_sprawdzenia)}] Przeglądam: {plik['url']}"
        f" ({plik['nazwa']})"
    )

    status_sukces = False
    wyniki = []

    try:
      odpowiedz = requests.get(plik["url"], headers=HEADERS, timeout=15)
      if odpowiedz.status_code == 200:
        with open("temp.pdf", "wb") as f:
          f.write(odpowiedz.content)

        wyniki = analizuj_pdf("temp.pdf")
        status_sukces = True
        print(f"-> Zakończono analizę pliku. Znaleziono trafień: {len(wyniki)}")
      else:
        print(f"-> Błąd HTTP: {odpowiedz.status_code}")
    except Exception as e:
      print(f"-> Błąd pobierania: {e}")

    raport_przegladu.append({
        "nazwa": plik["nazwa"],
        "url": plik["url"],
        "sukces": status_sukces,
        "dane": wyniki,
    })

  if os.path.exists("temp.pdf"):
    os.remove("temp.pdf")

  print("\nTworzenie pliku index.html...")

  html = f"""<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Plan Zajęć - {SZUKANA_GRUPA}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f9; color: #333; }}
        h1 {{ text-align: center; color: #2c3e50; }}
        p.sub {{ text-align: center; color: #7f8c8d; font-size: 0.9em; }}
        .sekcja-pliku {{ margin-top: 25px; padding: 20px; background: #fff; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); border-top: 5px solid #3498db; }}
        .tytul-pliku {{ font-size: 1.15em; font-weight: bold; margin-bottom: 5px; }}
        .url-info {{ font-size: 0.9em; color: #7f8c8d; margin-bottom: 15px; word-break: break-all; }}
        .url-info a {{ color: #2980b9; text-decoration: none; font-weight: bold; }}
        .url-info a:hover {{ text-decoration: underline; }}
        .karta {{ background-color: #f8f9fa; border: 1px solid #e9ecef; border-radius: 5px; padding: 15px; margin-bottom: 10px; }}
        .naglowek {{ font-size: 0.85em; color: #7f8c8d; margin-bottom: 8px; font-weight: bold; }}
        .zajecia {{ font-size: 1.05em; color: #2c3e50; font-weight: bold; }}
        .brak-danych {{ color: #e67e22; font-size: 0.9em; font-style: italic; }}
    </style>
</head>
<body>
    <h1>Plan Zajęć: {SZUKANA_GRUPA}</h1>
    <p class="sub">Automatyczny monitoring ze strefy słuchacza TEB Poznań.</p>
"""

  if raport_przegladu:
    for poz in raport_przegladu:
      html += f"""    <div class="sekcja-pliku">
        <div class="tytul-pliku">📄 {poz['nazwa']}</div>
        <div class="url-info">Link bezpośredni do pliku PDF: <a href="{poz['url']}" target="_blank">{poz['url']}</a></div>
"""
      if poz["dane"]:
        for wynik in poz["dane"]:
          html += f"""        <div class="karta">
            <div class="naglowek">Strona {wynik['strona']} | Wykryte godziny:<br>{wynik['naglowki']}</div>
            <div class="zajecia">Zajęcia / Sala:<br>{wynik['zajecia']}</div>
        </div>
"""
      else:
        html += (
            '        <div class="karta brak-danych">Brak zajęć dla tej grupy w'
            " tym dokumencie.</div>\n"
        )

      html += "    </div>\n"
  else:
    html += (
        '<p style="text-align:center; color: red; margin-top: 40px;">Nie udało'
        " się pobrać linków ze strony głównej.</p>"
    )

  html += """</body>
</html>"""

  with open("index.html", "w", encoding="utf-8") as f:
    f.write(html)
  print("Zakończono sukcesem! Utworzono plik index.html.")


if __name__ == "__main__":
  generuj_strone()
