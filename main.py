import pdfplumber
import requests
from bs4 import BeautifulSoup
import urllib.parse
import os

URL_STRONY = "https://teb.pl/oddzialy/d/poznan/strefa-sluchacza/"
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
SZUKANA_GRUPA = "I Technik masażysta_we"

def pobierz_wszystkie_linki_z_ul():
    print(f"Pobieranie strony: {URL_STRONY} ...")
    try:
        response = requests.get(URL_STRONY, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"Błąd podczas pobierania strony: {e}")
        return []
    
    soup = BeautifulSoup(response.text, 'html.parser')
    linki = []
    
    # Selektor wskazujący na wybraną listę UL
    selektor_ul = "body > div.root > div.page.page-departments.subpage-downloads > section.files > div > div:nth-of-type(1) > div > ul"
    ul_element = soup.select_one(selektor_ul)
    
    # Zapasowy selektor na przypadek różnic w strukturze klas
    if not ul_element:
        print("Nie znaleziono ścieżki pełnej. Używam selektora zapasowego...")
        ul_element = soup.select_one("section.files div.container > div:nth-of-type(1) ul")

    if not ul_element:
        print("Nie udało się zlokalizować listy UL na stronie.")
        return []

    # Wyciągamy WSZYSTKIE odnośniki <a> bez przefiltrowywania po nazwach
    elementy_a = ul_element.find_all('a')

    for a in elementy_a:
        href = a.get('href')
        nazwa_pliku = a.text.strip() or "Dokument PDF"
        
        # Bierzemy każdy link kończący się na .pdf
        if href and href.lower().endswith('.pdf'):
            pelny_url = urllib.parse.urljoin(URL_STRONY, href)
            linki.append({"nazwa": nazwa_pliku, "url": pelny_url})
            
    print(f"Wykryto łącznie {len(linki)} plików PDF na liście. Wszystkie zostaną przetworzone.")
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
                            "naglowki": " ".join(aktualne_naglowki) if aktualne_naglowki else "Brak wykrytych nagłówków",
                            "zajecia": zajecia_tekst if zajecia_tekst else "Dzień wolny (brak zajęć)"
                        })
    except Exception as e:
        print(f"Błąd odczytu pliku PDF: {e}")
        
    return wyniki_pdf

def generuj_strone():
    pliki_do_sprawdzenia = pobierz_wszystkie_linki_z_ul()
    wszystkie_wyniki = {}

    for plik in pliki_do_sprawdzenia:
        print(f"Pobieranie i analiza: {plik['nazwa']}...")
        try:
            odpowiedz = requests.get(plik['url'], headers=HEADERS, timeout=15)
            if odpowiedz.status_code == 200:
                with open("temp.pdf", "wb") as f:
                    f.write(odpowiedz.content)
                    
                wyniki = analizuj_pdf("temp.pdf")
                wszystkie_wyniki[plik['nazwa']] = {
                    "url": plik['url'],
                    "dane": wyniki
                }
        except Exception as e:
            print(f"Pominięto {plik['nazwa']} z powodu błędu pobierania: {e}")
        
    if os.path.exists("temp.pdf"):
        os.remove("temp.pdf")

    print("Tworzenie pliku index.html...")
    
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
        .tytul-pliku {{ font-size: 1.15em; font-weight: bold; margin-bottom: 15px; }}
        .tytul-pliku a {{ color: #2980b9; text-decoration: none; }}
        .tytul-pliku a:hover {{ text-decoration: underline; }}
        .karta {{ background-color: #f8f9fa; border: 1px solid #e9ecef; border-radius: 5px; padding: 15px; margin-bottom: 10px; }}
        .naglowek {{ font-size: 0.85em; color: #7f8c8d; margin-bottom: 8px; font-weight: bold; }}
        .zajecia {{ font-size: 1.05em; color: #2c3e50; font-weight: bold; }}
        .brak-danych {{ color: #e74c3c; font-size: 0.9em; }}
    </style>
</head>
<body>
    <h1>Plan Zajęć: {SZUKANA_GRUPA}</h1>
    <p class="sub">Wszystkie pliki pobrane bez filtrowania z sekcji planów na stronie TEB.</p>
"""

    if wszystkie_wyniki:
        for nazwa_pliku, zawartosc in wszystkie_wyniki.items():
            html += f"""    <div class="sekcja-pliku">
        <div class="tytul-pliku">📄 <a href="{zawartosc['url']}" target="_blank">{nazwa_pliku}</a></div>
"""
            if zawartosc['dane']:
                for wynik in zawartosc['dane']:
                    html += f"""        <div class="karta">
            <div class="naglowek">Godziny (Oś czasu):<br>{wynik['naglowki']}</div>
            <div class="zajecia">Zajęcia / Sala:<br>{wynik['zajecia']}</div>
        </div>
"""
            else:
                html += '        <div class="karta brak-danych">Nie znaleziono wiersza dla tej grupy w tym pliku.</div>\n'
                
            html += "    </div>\n"
    else:
        html += '<p style="text-align:center; color: red;">Brak jakichkolwiek plików PDF na liście.</p>'

    html += """</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Zakończono sukcesem!")

if __name__ == "__main__":
    generuj_strone()
