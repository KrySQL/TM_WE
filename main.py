import os
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import pdfplumber
import requests
from playwright.sync_api import sync_playwright

URL_STRONY = "https://teb.pl/oddzialy/d/poznan/strefa-sluchacza/"

def pobierz_liste_pdfow():
    print("Uruchamianie przeglądarki Playwright w celu ominięcia Cloudflare...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        try:
            print(f"Otwieranie strony: {URL_STRONY}")
            page.goto(URL_STRONY, timeout=60000)
            page.wait_for_timeout(6000)
            html_content = page.content()
            print(f"Pobrano kod HTML przez Playwright, długość: {len(html_content)} znaków")
        except Exception as e:
            print(f"Błąd podczas ładowania strony przez Playwright: {e}")
            browser.close()
            return []
            
        browser.close()

    soup = BeautifulSoup(html_content, 'html.parser')
    
    znalezione_linki = set()
    for a in soup.find_all('a', href=True):
        href = a['href']
        pelny_url = urljoin(URL_STRONY, href)
        
        if pelny_url.startswith("https://teb.pl/wp-content/uploads/poznan/") and pelny_url.endswith(".pdf"):
            znalezione_linki.add(pelny_url)

    lista_pdfow = sorted(list(znalezione_linki))
    print(f"Znaleziono unikalnych plików PDF: {len(lista_pdfow)}")
    for link in lista_pdfow:
        print(f" -> {link}")
        
    return lista_pdfow

def przetworz_pdfy():
    pdf_linki = pobierz_liste_pdfow()
    wyniki = []
    
    if not pdf_linki:
        print("Brak linków do przetworzenia!")
        return wyniki
    
    TOLERANCJA_Y = 5 
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

    for idx, url in enumerate(pdf_linki):
        nazwa_pliku_tymczasowa = f"plan_{idx}.pdf"
        print(f"Pobieranie pliku: {url}")
        
        try:
            resp = requests.get(url, headers=headers)
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
                    
                    # Grupowanie słów w rzędy (oś Y)
                    rzedy = {}
                    for slowo in slowa:
                        y = round(slowo['top'] / TOLERANCJA_Y) * TOLERANCJA_Y
                        if y not in rzedy:
                            rzedy[y] = []
                        rzedy[y].append(slowo)
                        
                    posortowane_wysokosci = sorted(rzedy.keys())
                    aktualne_naglowki = []
                    
                    for y in posortowane_wysokosci:
                        wiersz_slowa = sorted(rzedy[y], key=lambda w: w['x0'])
                        teksty_wiersza = [w['text'] for w in wiersz_slowa]
                        pelny_tekst_spacja = " ".join(teksty_wiersza)
                        
                        # Wykrywanie kolumn na podstawie odstępów poziomu X
                        kolumny = []
                        aktualna_kolumna = []
                        ostatni_x1 = None
                        
                        for slowo in wiersz_slowa:
                            if ostatni_x1 is not None and (slowo['x0'] - ostatni_x1) > 15:
                                if aktualna_kolumna:
                                    kolumny.append(" ".join(aktualna_kolumna))
                                    aktualna_kolumna = []
                            aktualna_kolumna.append(slowo['text'])
                            ostatni_x1 = slowo['x1']
                        if aktualna_kolumna:
                            kolumny.append(" ".join(aktualna_kolumna))
                            
                        sformatowany_wiersz = " || ".join(kolumny)
                        
                        if "semestr / grupa" in pelny_tekst_spacja.lower() or "8:00" in pelny_tekst_spacja:
                            aktualne_naglowki = teksty_wiersza
                            
                        # Szukanie grupy
                        if "I" in teksty_wiersza and "Technik" in teksty_wiersza and ("masażysta_we" in teksty_wiersza or "masażysta" in teksty_wiersza):
                            zajecia_tekst = sformatowany_wiersz.replace("I Technik masażysta_we", "").replace("I Technik masażysta", "").strip(" |")
                            
                            if not aktualne_naglowki:
                                aktualne_naglowki = ["Brak nagłówków"]
                                
                            wyniki.append({
                                "plik_url": url,
                                "nazwa_pliku": os.path.basename(url),
                                "strona": nr_strony + 1,
                                "naglowki": " ".join(aktualne_naglowki),
                                "zajecia": zajecia_tekst if zajecia_tekst else "Dzień wolny (brak przydzielonych zajęć/sal)"
                            })
        except Exception as e:
            print(f"Błąd przetwarzania PDF {nazwa_pliku_tymczasowa}: {e}")

    return wyniki

def generuj_html():
    wyniki = przetworz_pdfy()
    
    print("Generowanie pliku index.html...")
    
    html = """<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Plan Zajęć - I Technik masażysta_we</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f9; color: #333; }
        h1 { text-align: center; color: #2c3e50; margin-bottom: 25px; }
        .karta { background-color: #fff; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); padding: 20px; margin-bottom: 20px; border-left: 5px solid #3498db; }
        .karta-pusta { background-color: #fff; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); padding: 20px; margin-bottom: 20px; border-left: 5px solid #e74c3c; text-align: center; }
        .naglowek { font-size: 0.85em; color: #7f8c8d; margin-bottom: 10px; font-weight: bold; }
        .zajecia { font-size: 1.05em; color: #2c3e50; font-weight: bold; line-height: 1.5; }
        .strona-info { font-size: 0.8em; color: #bdc3c7; margin-top: 10px; text-align: right; }
        a { color: #2980b9; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <h1>Plan Zajęć: I Technik masażysta_we</h1>
"""

    if wyniki:
        for wynik in wyniki:
            html += f"""    <div class="karta">
        <div class="naglowek">Plik źródłowy: <a href="{wynik['plik_url']}" target="_blank">{wynik['nazwa_pliku']}</a> | Wykryte godziny: {wynik['naglowki']}</div>
        <div class="zajecia">{wynik['zajecia']}</div>
        <div class="strona-info">Znaleziono na stronie {wynik['strona']}</div>
    </div>
"""
    else:
        html += '    <div class="karta-pusta"><div class="zajecia">Nie znaleziono pasujących zajęć dla grupy „I Technik masażysta_we” w pobranym okresie.</div></div>\n'

    html += """</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Zakończono sukcesem!")

if __name__ == "__main__":
    generuj_html()
