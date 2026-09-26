import pdfplumber
import requests

# Adres URL do pliku PDF
URL = "https://teb.pl/wp-content/uploads/poznan/2026/09/plan-salek-25.09.pdf"

def pobierz_pdf():
    print("Pobieranie pliku PDF...")
    # Dodajemy nagłówki przeglądarki, bo niektóre serwery blokują automatyczne skrypty
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
    response = requests.get(URL, headers=headers)
    with open("plan.pdf", "wb") as f:
        f.write(response.content)

def generuj_html():
    pobierz_pdf()
    print("Przeszukiwanie tekstu w PDF...")
    
    wyniki = []
    aktualne_naglowki = []
    
    # Tolerancja pionowa w punktach - decyduje, czy tekst na lekko innej wysokości
    # ma zostać potraktowany jako ten sam wiersz tabeli.
    TOLERANCJA_Y = 5 

    with pdfplumber.open("plan.pdf") as pdf:
        for nr_strony, strona in enumerate(pdf.pages):
            # Wyciągamy każde pojedyncze słowo ze strony wraz ze współrzędnymi
            slowa = strona.extract_words()
            
            # Grupowanie słów w rzędy na podstawie osi Y
            rzedy = {}
            for slowo in slowa:
                y = round(slowo['top'] / TOLERANCJA_Y) * TOLERANCJA_Y
                tekst = slowo['text']
                if y not in rzedy:
                    rzedy[y] = []
                rzedy[y].append(tekst)
                
            # Zbieranie ułożonych rzędów
            posortowane_wysokosci = sorted(rzedy.keys())
            
            for y in posortowane_wysokosci:
                wiersz = rzedy[y]
                pelny_tekst = " ".join(wiersz)
                
                # Zapisywanie rzędu z nagłówkami
                if "semestr / grupa" in pelny_tekst.lower() or "8:00" in pelny_tekst:
                    # Łączymy wszystkie fragmenty godzin w jeden ciąg, żeby to łatwiej zrzucić
                    aktualne_naglowki = wiersz
                    
                # Szukanie konkretnej grupy
                if "I" in wiersz and "Technik" in wiersz and ("masażysta_we" in wiersz or "masażysta" in wiersz):
                    
                    # Usunięcie z przodu słów "I Technik masażysta_we", żeby zostały same zajęcia
                    zajecia_tekst = pelny_tekst.replace("I Technik masażysta_we", "").replace("I Technik masażysta", "").strip()
                    
                    # Zabezpieczenie na wypadek, gdyby nagłówków nie znaleziono
                    if not aktualne_naglowki:
                        aktualne_naglowki = ["Brak nagłówków (sprawdź oryginalny plik)"]
                        
                    wyniki.append({
                        "strona": nr_strony + 1,
                        "naglowki": " ".join(aktualne_naglowki),
                        "zajecia": zajecia_tekst if zajecia_tekst else "Dzień wolny (brak przydzielonych zajęć/sal)"
                    })

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
        .naglowek { font-size: 0.9em; color: #7f8c8d; margin-bottom: 10px; font-weight: bold; }
        .zajecia { font-size: 1.1em; color: #2c3e50; font-weight: bold; }
        .strona-info { font-size: 0.8em; color: #bdc3c7; margin-top: 10px; text-align: right; }
    </style>
</head>
<body>
    <h1>Plan Zajęć: I Technik masażysta_we</h1>
"""

    if wyniki:
        for wynik in wyniki:
            html += f"""    <div class="karta">
        <div class="naglowek">Wykryte godziny (Oś czasu z dokumentu):<br>{wynik['naglowki']}</div>
        <div class="zajecia">Wykryte zajęcia/sale:<br>{wynik['zajecia']}</div>
        <div class="strona-info">Znaleziono na stronie {wynik['strona']}</div>
    </div>
"""
    else:
        html += '    <div class="karta"><div class="zajecia">Nie znaleziono zajęć dla tej grupy. Być może format pliku PDF znacznie się zmienił.</div></div>\n'

    html += """</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Zakończono sukcesem!")

if __name__ == "__main__":
    generuj_html()
