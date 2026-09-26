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
    
    try:
        response = requests.get(STRONA_GLOWNA, headers=headers)
        response.raise_for_status()
    except Exception as e:
        print(f"Błąd pobierania strony głównej: {e}")
        return []
    
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Pobieramy absolutnie wszystkie tagi <a> ze strony
    wszystkie_tagi_a = soup.find_all('a', href=True)
    
    pdf_linki = []
    widzialne_url = set() # Do unikania duplikatów
    
    print("\n--- ANALIZA WSZYSTKICH LINKÓW NA STRONIE ---")
    for index, a in enumerate(wszystkie_tagi_a, 1):
        href = a.get('href').strip()
        
        # Sprawdzamy, czy link spełnia wytyczne użytkownika:
        # 1. Zaczyna się na "https://teb.pl/wp-content/uploads/poznan/"
        # 2. Kończy się na ".pdf" (bierzemy pod uwagę małe/wielkie litery)
        if href.startswith("https://teb.pl/wp-content/uploads/poznan/") and href.lower().endswith(".pdf"):
            if href not in widzialne_url:
                widzialne_url.add(href)
                
                # Próbujemy wyciągnąć czytelną nazwę pliku z zawartości span.filename lub tekstu linku
                span_filename = a.find('span', class_='filename')
                if span_filename and span_filename.text.strip():
                    tytul = span_filename.text.strip()
                elif a.text.strip():
                    tytul = a.text.strip()
                else:
                    tytul = href.split('/')[-1]
                
                print(f"[PASUJE] [{index}] {tytul} -> {href}")
                pdf_linki.append({
                    'url': href,
                    'tytul': tytul
                })
            else:
                print(f"[POMINIĘTO DUPLIKAT] {href}")
        else:
            # Możesz odkomentować poniższą linię, jeśli chcesz widzieć absolutnie każdy odrzucony link
            # print(f"[ODRZUCONY] {href}")
            pass
            
    print("-" * 50)
    print(f"Łącznie zakwalifikowano {len(pdf_linki)} unikalnych plików PDF do pobrania.")
    return pdf_linki

def przetworz_pdf(url, tytul_dokumentu):
    print(f"Pobieranie i przetwarzanie pliku: {tytul_dokumentu}...")
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
                    
                    # Wyszukiwanie nagłówków z godzinami
                    if "semestr / grupa" in pelny_tekst.lower() or "8:00" in pelny_tekst:
                        aktualne_naglowki = wiersz
                        
                    # Szukanie grupy docelowej
                    if "I" in wiersz and "Technik" in wiersz and ("masażysta_we" in wiersz or "masażysta" in wiersz):
                        zajecia_tekst = pelny_tekst.replace("I Technik masażysta_we", "").replace("I Technik masażysta", "").strip()
                        
                        if not aktualne_naglowki:
                            aktualne_naglowki = ["Brak nagłówków godzinowych"]
                            
                        wyniki.append({
                            "dokument": tytul_dokumentu,
                            "strona": nr_strony + 1,
                            "naglowki": " ".join(aktualne_naglowki),
                            "zajecia": zajecia_tekst if zajecia_tekst else "Dzień wolny (brak przydzielonych zajęć/sal)"
                        })
    except Exception as e:
        print(f"Błąd analizy pliku PDF {tytul_dokumentu}: {e}")
        
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
        body { font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f9; color: #333; max-width: 900px; margin-left: auto; margin-right: auto; }
        h1, h2 { text-align: center; color: #2c3e50; }
        .sekcja-pliki { background-color: #fff; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); padding: 20px; margin-bottom: 30px; border-left: 5px solid #27ae60; }
        .sekcja-pliki ul { padding-left: 20px; margin: 10px 0; }
        .sekcja-pliki li { margin-bottom: 8px; word-break: break-all; }
        .sekcja-pliki a { color: #2980b9; text-decoration: none; font-weight: bold; }
        .sekcja-pliki a:hover { text-decoration: underline; }
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
    
    <div class="sekcja-pliki">
        <h2>Wszystkie znalezione i przetworzone pliki PDF</h2>
"""

    if pdf_linki:
        html += "        <ul>\n"
        for item in pdf_linki:
            html += f"            <li>📄 <strong>{item['tytul']}</strong><br><a href=\"{item['url']}\" target=\"_blank\">{item['url']}</a></li>\n"
        html += "        </ul>\n"
    else:
        html += "        <p>Nie znaleziono pasujących plików PDF spełniających kryteria URL.</p>\n"

    html += """    </div>

    <h2>Wyniki wyszukiwania zajęć i sal dla grupy</h2>
"""

    if wszystkie_wyniki:
        for wynik in wszystkie_wyniki:
            html += f"""    <div class="karta">
        <div class="dokument-tytul">📄 Źródło: {wynik['dokument']}</div>
        <div class="naglowek">Wykryte godziny (Oś czasu z dokumentu):<br>{wynik['naglowki']}</div>
        <div class="zajecia">Wykryte zajęcia/sale:<br>{wynik['zajecia']}</div>
        <div class="strona-info">Znaleziono na stronie {wynik['strona']}</div>
    </div>
"""
    else:
        html += '    <div class="brak">Nie znaleziono pasujących zajęć dla tej grupy w przeszukanych dokumentach.</div>\n'

    html += """</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Zakończono sukcesem! Utworzono plik index.html.")

if __name__ == "__main__":
    generuj_html()
