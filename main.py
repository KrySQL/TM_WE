import pdfplumber
import requests
from bs4 import BeautifulSoup
import urllib.parse
import os

# Konfiguracja
URL_STRONY = "https://teb.pl/oddzialy/d/poznan/strefa-sluchacza/"
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
SZUKANA_GRUPA = "I Technik masażysta_we"

def pobierz_linki_pdf():
    print(f"Pobieranie strony: {URL_STRONY} ...")
    response = requests.get(URL_STRONY, headers=HEADERS)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.text, 'html.parser')
    linki = []
    
    # Używamy ścieżki zbliżonej do podanej (BeautifulSoup korzysta z nth-of-type zamiast nth-child dla bezpieczeństwa)
    selektor = "section.files > div > div:nth-of-type(1) > div > ul > li > a"
    elementy_a = soup.select(selektor)
    
    # Zabezpieczenie: jeśli dokładny selektor nie zadziała (bo surowy HTML różni się od tego w oknie przeglądarki),
    # używamy prostszego szukania pierwszej listy plików na stronie.
    if not elementy_a:
        print("Nie znaleziono linków z użyciem dokładnej ścieżki. Używam selektora zapasowego...")
        elementy_a = soup.select("section.files ul li a")

    for a in elementy_a:
        href = a.get('href')
        if href and href.lower().endswith('.pdf'):
            nazwa_pliku = a.text.strip() or "Brak nazwy"
            pelny_url = urllib.parse.urljoin(URL_STRONY, href)
            linki.append({"nazwa": nazwa_pliku, "url": pelny_url})
            
    print(f"Znaleziono {len(linki)} plików PDF do analizy.")
    return linki

def analizuj_pdf(sciezka_pdf):
    wyniki_pdf = []
    TOLERANCJA_Y = 5 

    try:
        with pdfplumber.open(sciezka_pdf) as pdf:
            for nr_strony, strona in enumerate(pdf.pages):
                slowa = strona.extract_words()
                rzedy = {}
                for slowo in slowa:
                    y = round(slowo['top'] / TOLERANCJA_Y) * TOLERANCJA_Y
                    if y not in rzedy:
                        rzedy[y] = []
                    rzedy[y].append(slowo['text'])
                    
                posortowane_wysokosci = sorted(rzedy.keys())
                aktualne_naglowki = []
                
                for y in posortowane_wysokosci:
                    wiersz = rzedy[y]
                    pelny_tekst = " ".join(wiersz)
                    
                    if "semestr / grupa" in pelny_tekst.lower() or "8:00" in pelny_tekst:
                        aktualne_naglowki = wiersz
                        
                    if "I" in wiersz and "Technik" in wiersz and ("masażysta_we" in wiersz or "masażysta" in wiersz):
                        zajecia_tekst = pelny_tekst.replace("I Technik masażysta_we", "").replace("I Technik masażysta", "").strip()
                        
                        wyniki_pdf.append({
                            "strona": nr_strony + 1,
                            "naglowki": " ".join(aktualne_naglowki) if aktualne_naglowki else "Brak nagłówków (sprawdź plik)",
                            "zajecia": zajecia_tekst if zajecia_tekst else "Dzień wolny (brak zajęć/sal)"
                        })
    except Exception as e:
        print(f"Błąd odczytu pliku: {e}")
        
    return wyniki_pdf

def generuj_strone():
    pliki_do_sprawdzenia = pobierz_linki_pdf()
    wszystkie_wyniki = {}

    for plik in pliki_do_sprawdzenia:
        print(f"Pobieranie i analiza: {plik['nazwa']} ({plik['url']})")
        odpowiedz = requests.get(plik['url'], headers=HEADERS)
        
        with open("temp.pdf", "wb") as f:
            f.write(odpowiedz.content)
            
        wyniki = analizuj_pdf("temp.pdf")
        wszystkie_wyniki[plik['nazwa']] = {
            "url": plik['url'],
            "dane": wyniki
        }
        
    # Sprzątanie tymczasowego pliku
    if os.path.exists("temp.pdf"):
        os.remove("temp.pdf")

    print("Generowanie pliku index.html...")
    
    html = f"""<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Plan Zajęć - {SZUKANA_GRUPA}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f9; color: #333; }}
        h1 {{ text-align: center; color: #2c3e50; }}
        .sekcja-pliku {{ margin-top: 30px; padding: 20px; background: #fff; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); border-top: 5px solid #3498db; }}
        .tytul-pliku {{ font-size: 1.2em; font-weight: bold; margin-bottom: 15px; }}
        .tytul-pliku a {{ color: #2980b9; text-decoration: none; }}
        .tytul-pliku a:hover {{ text-decoration: underline; }}
        .karta {{ background-color: #f8f9fa; border: 1px solid #e9ecef; border-radius: 5px; padding: 15px; margin-bottom: 10px; }}
        .naglowek {{ font-size: 0.85em; color: #7f8c8d; margin-bottom: 8px; font-weight: bold; }}
        .zajecia {{ font-size: 1.05em; color: #2c3e50; font-weight: bold; }}
        .brak-danych {{ color: #e74c3c; font-weight: bold; }}
    </style>
</head>
<body>
    <h1>Plan Zajęć: {SZUKANA_GRUPA}</h1>
    <p style="text-align:center;">Automatyczne podsumowanie ze wszystkich plików w strefie słuchacza (Poznań).</p>
"""

    for nazwa_pliku, zawartosc in wszystkie_wyniki.items():
        html += f"""    <div class="sekcja-pliku">
        <div class="tytul-pliku">📄 <a href="{zawartosc['url']}" target="_blank">{nazwa_pliku}</a></div>
"""
        if zawartosc['dane']:
            for wynik in zawartosc['dane']:
                html += f"""        <div class="karta">
            <div class="naglowek">Godziny:<br>{wynik['naglowki']}</div>
            <div class="zajecia">Zajęcia/Sale:<br>{wynik['zajecia']}</div>
        </div>
"""
        else:
            html += '        <div class="karta brak-danych">Nie znaleziono zajęć dla Twojej grupy w tym pliku.</div>\n'
            
        html += "    </div>\n"

    html += """</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Zakończono sukcesem!")

if __name__ == "__main__":
    generuj_strone()
