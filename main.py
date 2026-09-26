import os
from urllib.parse import urljoin
import cloudscraper
from bs4 import BeautifulSoup
import pdfplumber

# Adres strony ze strefą słuchacza TEB Poznań
URL_STRONY = "https://teb.pl/oddzialy/d/poznan/strefa-sluchacza/"

def pobierz_liste_pdfow():
    print("Pobieranie listy plikow ze strony TEB...")
    
    # Tworzymy scraper odporny na zabezpieczenia Cloudflare
    scraper = cloudscraper.create_scraper()
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        response = scraper.get(URL_STRONY, headers=headers)
        response.raise_for_status()
    except Exception as e:
        print(f"Błąd podczas pobierania strony: {e}")
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    znalezione_linki = set()

    # Przeszukujemy każdy tag <a> na stronie
    for a in soup.find_all('a', href=True):
        href = a['href']
        # Pełny URL (gdyby link był względny)
        pelny_url = urljoin(URL_STRONY, href)
        
        # Filtrowanie według Twoich wytycznych:
        # musi zaczynać się na wskazany adres i kończyć na .pdf
        if pelny_url.startswith("https://teb.pl/wp-content/uploads/poznan/") and pelny_url.endswith(".pdf"):
            znalezione_linki.add(pelny_url)

    lista_pdfow = sorted(list(znalezione_linki))
    print(f"Znaleziono unikalnych pliku PDF: {len(lista_pdfow)}")
    for link in lista_pdfow:
        print(f" -> {link}")
        
    return lista_pdfow

def przetworz_pdfy():
    pdf_Linki = pobierz_liste_pdfow()
    wyniki = []
    
    TOLERANCJA_Y = 5 
    scraper = cloudscraper.create_scraper()
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

    for idx, url in enumerate(pdf_Linki):
        nazwa_pliku_tymczasowa = f"plan_{idx}.pdf"
        print(f"Pobieranie pliku: {url}")
        
        try:
            resp = scraper.get(url, headers=headers)
            with open(nazwa_pliku_tymczasowa, "wb") as f:
                f.write(resp.content)
        except Exception as e:
            print(f"Nie udało się pobrać {url}: {e}")
            continue

        print(f"Przeszukiwanie tekstu w {nazwa_pliku_tymczasowa}...")
        
        try:
            with pdfplumber.open(nazwa_pliku_tymczasowa) as pdf:
                for nr_strony, strona in enumerate(pdf.pages):
                    slowa = strona.extract_words()
                    
                    rzedy = {}
                    for slowo in slowa:
                        y = round(slowo['top'] / TOLERANCJA_Y) * TOLERANCJA_Y
                        tekst = slowo['text']
                        if y not in rzedy:
                            rzedy[y] = []
                        rzedy[y].append(tekst)
                        
                    posortowane_wysokosci = sorted(rzedy.keys())
                    aktualne_naglowki = []
                    
                    for y in posortowane_wysokosci:
                        wiersz = rzedy[y]
                        pelny_tekst = " ".join(wiersz)
                        
                        if "semestr / grupa" in pelny_tekst.lower() or "8:00" in pelny_tekst:
                            aktualne_naglowki = wiersz
                            
                        # Szukanie konkretnej grupy
                        if "I" in wiersz and "Technik" in wiersz and ("masażysta_we" in wiersz or "masażysta" in wiersz):
                            zajecia_tekst = pelny_tekst.replace("I Technik masażysta_we", "").replace("I Technik masażysta", "").strip()
                            
                            if not aktualne_naglowki:
                                aktualne_naglowki = ["Brak nagłówków (sprawdź oryginalny plik)"]
                                
                            wyniki.append({
                                "plik_url": url,
                                "nazwa_pliku": os.path.basename(url),
                                "strona": nr_strony + 1,
                                "naglowki": " ".join(aktualne_naglowki),
                                "zajecia": zajecia_tekst if zajecia_tekst else "Dzień wolny (brak przydzielonych zajęć/sal)"
                            })
        except Exception as e:
            print(f"Błąd przetwarzania PDF {nazwa_pliku_tymczasowa}: {e}")

    return pdf_Linki, wyniki

def generuj_html():
    pobrane_linki, wyniki = przetworz_pdfy()
    
    print("Generowanie pliku index.html...")
    
    html = """<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Plan Zajęć - I Technik masażysta_we</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f9; color: #333; }
        h1, h2 { text-align: center; color: #2c3e50; }
        .karta { background-color: #fff; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); padding: 20px; margin-bottom: 20px; border-left: 5px solid #3498db; }
        .karta-lista { background-color: #fff; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); padding: 20px; margin-bottom: 25px; border-left: 5px solid #2ecc71; }
        .naglowek { font-size: 0.9em; color: #7f8c8d; margin-bottom: 10px; font-weight: bold; }
        .zajecia { font-size: 1.1em; color: #2c3e50; font-weight: bold; }
        .strona-info { font-size: 0.8em; color: #bdc3c7; margin-top: 10px; text-align: right; }
        ul { margin: 0; padding-left: 20px; }
        li { margin-bottom: 6px; }
        a { color: #2980b9; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <h1>Plan Zajęć: I Technik masażysta_we</h1>
    
    <div class="karta-lista">
        <h2>Wykryte pliki PDF ze strony TEB:</h2>
"""
    if pobrane_linki:
        html += "        <ul>\n"
        for link in pobrane_linki:
            html += f'            <li><a href="{link}" target="_blank">{link}</a></li>\n'
        html += "        </ul>\n"
    else:
        html += "        <p>Nie znaleziono żadnych pasujących plików PDF na stronie.</p>\n"
    
    html += "    </div>\n\n    <h2>Wyniki wyszukiwania zajęć:</h2>\n"

    if wyniki:
        for wynik in wyniki:
            html += f"""    <div class="karta">
        <div class="naglowek">Plik źródłowy: <a href="{wynik['plik_url']}" target="_blank">{wynik['nazwa_pliku']}</a><br>Wykryte godziny: {wynik['naglowki']}</div>
        <div class="zajecia">Wykryte zajęcia/sale: {wynik['zajecia']}</div>
        <div class="strona-info">Znaleziono na stronie {wynik['strona']}</div>
    </div>
"""
    else:
        html += '    <div class="karta"><div class="zajecia">Nie znaleziono zajęć dla tej grupy w przetworzonych dokumentach.</div></div>\n'

    html += """</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Zakończono sukcesem!")

if __name__ == "__main__":
    generuj_html()
