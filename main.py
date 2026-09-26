import pdfplumber
import requests
import os

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
    
    znalezione_zajecia = []
    
    # pdfplumber świetnie radzi sobie z siatką tabel w dokumentach PDF
    with pdfplumber.open("plan.pdf") as pdf:
        for strona in pdf.pages:
            tabele = strona.extract_tables()
            for tabela in tabele:
                for wiersz in tabela:
                    # Oczyszczenie wiersza - usunięcie pustych wartości (None) oraz znaków nowej linii
                    oczyszczony_wiersz = [str(komorka).strip().replace('\n', ' ') if komorka else "" for komorka in wiersz]
                    
                    # Sprawdzenie czy wiersz nie jest pusty i czy w 1 kolumnie jest nazwa grupy
                    if len(oczyszczony_wiersz) > 0 and "I Technik masażysta_we" in oczyszczony_wiersz[0]:
                        znalezione_zajecia.append(oczyszczony_wiersz)

    print("Generowanie pliku index.html...")
    
    # Prosty szablon HTML do wyświetlenia wyników
    html = """<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Plan Zajęć - I Technik masażysta_we</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f9; color: #333; }
        h1 { text-align: center; color: #2c3e50; }
        table { border-collapse: collapse; width: 100%; max-width: 1000px; margin: 0 auto; background-color: #fff; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        th, td { border: 1px solid #ddd; padding: 12px; text-align: left; }
        th { background-color: #3498db; color: white; }
        tr:nth-child(even) { background-color: #f9f9f9; }
        .footer { text-align: center; margin-top: 20px; font-size: 0.9em; color: #777; }
    </style>
</head>
<body>
    <h1>Plan Zajęć: I Technik masażysta_we</h1>
    <table>
"""

    if znalezione_zajecia:
        for wiersz in znalezione_zajecia:
            html += "        <tr>\n"
            for komorka in wiersz:
                html += f"            <td>{komorka}</td>\n"
            html += "        </tr>\n"
    else:
        html += "        <tr><td>Nie znaleziono zajęć dla tej grupy w analizowanym pliku.</td></tr>\n"

    html += """    </table>
    <div class="footer">Strona wygenerowana automatycznie przez GitHub Actions.</div>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    
    print("Zakończono sukcesem!")

if __name__ == "__main__":
    generuj_html()
