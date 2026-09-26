import pdfplumber
import requests

# Adres URL do pliku PDF
URL = "https://teb.pl/wp-content/uploads/poznan/2026/09/plan-salek-25.09.pdf"

def pobierz_pdf():
    print("Pobieranie pliku PDF...")
    response = requests.get(URL)
    with open("plan.pdf", "wb") as f:
        f.write(response.content)

def generuj_html():
    pobierz_pdf()
    print("Przeszukiwanie tabel w PDF...")
    
    wyniki = []
    aktualne_naglowki = []
    
    with pdfplumber.open("plan.pdf") as pdf:
        for strona in pdf.pages:
            tabele = strona.extract_tables()
            for tabela in tabele:
                for wiersz in tabela:
                    # Oczyszczenie wiersza 
                    oczyszczony_wiersz = [str(komorka).strip().replace('\n', '') if komorka else "" for komorka in wiersz]
                    
                    if len(oczyszczony_wiersz) == 0:
                        continue
                        
                    # Przechwytujemy wiersz z godzinami 
                    if "semestr / grupa" in oczyszczony_wiersz[0]:
                        aktualne_naglowki = oczyszczony_wiersz
                        
                    # Szukamy zajęć i łączymy je z ostatnio zapamiętanymi nagłówkami
                    if "I Technik masażysta_we" in oczyszczony_wiersz[0]:
                        wyniki.append({
                            "naglowki": aktualne_naglowki,
                            "zajecia": oczyszczony_wiersz
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
        .tabela-kontener { overflow-x: auto; margin-bottom: 30px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        table { border-collapse: collapse; width: 100%; min-width: 1000px; background-color: #fff; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: center; font-size: 14px; }
        th { background-color: #3498db; color: white; min-width: 80px; }
        td { min-width: 80px; }
        tr:nth-child(even) { background-color: #f9f9f9; }
    </style>
</head>
<body>
    <h1>Plan Zajęć: I Technik masażysta_we</h1>
"""

    if wyniki:
        for wynik in wyniki:
            html += '    <div class="tabela-kontener">\n        <table>\n            <tr>\n'
            
            # Renderowanie nagłówków (godzin)
            for naglowek in wynik["naglowki"]:
                wartosc_naglowka = naglowek if naglowek else "-"
                html += f"                <th>{wartosc_naglowka}</th>\n"
            html += "            </tr>\n            <tr>\n"
            
            # Renderowanie zajęć
            for komorka in wynik["zajecia"]:
                # Zamiana całkowicie pustych komórek na estetyczny myślnik
                wartosc = komorka if komorka else "-"
                html += f"                <td>{wartosc}</td>\n"
            
            html += "            </tr>\n        </table>\n    </div>\n"
    else:
        html += "    <p style='text-align:center;'>Nie znaleziono zajęć dla tej grupy w analizowanym pliku.</p>\n"

    html += """</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Zakończono sukcesem!")

if __name__ == "__main__":
    generuj_html()
