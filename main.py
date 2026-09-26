import os
import requests
from bs4 import BeautifulSoup
import pdfplumber

# Adres strony ze strefą słuchacza TEB Poznań
STRONA_GLOWNA = "https://teb.pl/oddzialy/d/poznan/strefa-sluchacza/"

def pobierz_linki_pdf():
    print(f"Pobieranie listy plików z {STRONA_GLOWNA}...")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    response = requests.get(STRONA_GLOWNA, headers=headers)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Szukamy listy z plikami na podstawie struktury HTML sekcji plików
    elementy_a = soup.select("section.files ul.files-list li a")
    
    pdf_linki = []
    print("\n--- ZNALEZIONE LINKI W SEKCJI PLIKÓW ---")
    for index, a in enumerate(elementy_a, 1):
        href = a.get('href')
        span_filename = a.find('span', class_='filename')
        nazwa_pliku_tekst = span_filename.text if span_filename else a.text
        
        print(f"[{index}] Tytuł: {nazwa_pliku_tekst.strip()}")
        print(f"    Link: {href}")
        
        if href:
            pdf_linki.append({
                'url': href,
                'tytul': nazwa_pliku_tekst.strip()
            })
    print("----------------------------------------\n")
            
    print(f"Łącznie zakwalifikowano {len(pdf_linki)} plików do sprawdzenia.")
    return pdf_linki

def przetworz_pdf(url, tytul_dokumentu):
    print(f"Pobieranie i przetwarzanie pliku: {tytul_dokumentu} ({url})...")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
    except Exception as e:
        print(f"Błąd pobierania pliku {url}: {e}")
        return []
    
    nazwa_tymczasowa = "temp_plan.pdf"
    with open(nazwa_tymczasowa, "wb") as f:
        f.write(response.content)
        
    wyniki = []
    aktualne_naglowki = []
    TOLERANCJA_Y = 5 

    try:
        with pdfplumber.open(nazwa_tymczasowa) as pdf:
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
                
                for y in posortowane_wysokosci:
                    wiersz = rzedy[y]
                    pelny_tekst = " ".join(wiersz)
                    
                    if "semestr / grupa" in pelny_tekst.lower() or "8:00" in pelny_tekst:
                        aktualne_naglowki = wiersz
                        
                    if "I" in wiersz and "Technik" in wiersz and ("masażysta_we" in wiersz or "masażysta" in wiersz):
                        zajecia_tekst = pelny_tekst.replace("I Technik masażysta_we", "").replace("I Technik masażysta", "").strip()
                        
                        if not aktualne_naglowki:
                            aktualne_naglowki = ["Brak nagłówków (sprawdź oryginalny plik)"]
                            
                        wyniki.append({
                            "dokument": tytul_dokumentu,
                            "strona": nr_strony + 1,
                            "naglowki": " ".join(aktualne_naglowki),
                            "zajecia": zajecia_tekst if zajecia_tekst else "Dzień wolny (brak przydzielonych zajęć/sal)"
                        })
    except Exception as e:
        print(f"Błąd analizy PDF {tytul_dokumentu}: {e}")
        
    if os.path.exists(nazwa_tymczasowa):
        os.remove(nazwa_tymczasowa)
        
    return wyniki

def generuj_html():
    pdf_linki = pobierz_linki_pdf()
    wszystkie_wyniki = []

    for item in pdf_linki:
        wyniki_pliku = przetworz_pdf(item['url'], item['tytul'])
        wszystkie_wyniki.extend(wyniki_pliku)

    print("Generowanie pliku index.html...")
    
    html = """<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Plan Zajęć - I Technik masażysta_we</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f9; color: #333; }
        h1 { text-align: center; color: #2c3e50; }
        .karta { background-color: #fff; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); padding: 20px; margin-bottom: 20px; border-left: 5px solid #3498db; }
        .dokument-tytul { font-size: 1.1em; font-weight: bold; color: #e67e22; margin-bottom: 8px; border-bottom: 1px solid #eee; padding-bottom: 5px; }
        .naglowek { font-size: 0.9em; color: #7f8c8d; margin-bottom: 10px; font-weight: bold; }
        .zajecia { font-size: 1.1em; color: #2c3e50; font-weight: bold; }
        .strona-info { font-size: 0.8em; color: #bdc3c7; margin-top: 10px; text-align: right; }
        .brak { text-align: center; font-size: 1.2em; color: #7f8c8d; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    </style>
</head>
<body>
    <h1>Plan Zajęć: I Technik masażysta_we</h1>
"""

    if wszystkie_wyniki:
        for wynik in wszystkie_wyniki:
            html += f"""    <div class="karta">
        <div class="dokument-tytul">📄 {wynik['dokument']}</div>
        <div class="naglowek">Wykryte godziny (Oś czasu z dokumentu):<br>{wynik['naglowki']}</div>
        <div class="zajecia">Wykryte zajęcia/sale:<br>{wynik['zajecia']}</div>
        <div class="strona-info">Znaleziono na stronie {wynik['strona']}</div>
    </div>
"""
    else:
        html += '    <div class="brak">Nie znaleziono zajęć dla tej grupy w żadnym z pobranych planów.</div>\n'

    html += """</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Zakończono sukcesem! Utworzono index.html.")

if __name__ == "__main__":
    generuj_html()
