def pobierz_wszystkie_linki():
    print(f"Pobieranie strony: {URL_STRONY} ...")

    try:
        response = requests.get(URL_STRONY, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"Błąd podczas pobierania strony: {e}")
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    linki = []

    # Dokładnie wskazujemy sekcję <ul>, w której znajdują się dokumenty
    selektor_ul = (
        "body > div.root > div.page.page-departments.subpage-downloads "
        "> section.files > div > div:nth-child(1) > div > ul"
    )

    lista = soup.select_one(selektor_ul)

    if not lista:
        print("Nie znaleziono wskazanej listy <ul>.")
        return []

    # Pobieramy wszystkie <a> znajdujące się wewnątrz <li>
    elementy_a = lista.select("li a[href]")

    print(f"Znaleziono {len(elementy_a)} elementów <a> w tej liście.")

    for a in elementy_a:
        href = a.get("href")

        if not href:
            continue

        # Zamiana względnego adresu na pełny URL
        pelny_url = urllib.parse.urljoin(URL_STRONY, href)

        # Sprawdzamy rozszerzenie pliku, również gdy URL ma parametry
        sciezka_url = urllib.parse.urlparse(pelny_url).path

        if sciezka_url.lower().endswith(".pdf"):
            nazwa_pliku = a.get_text(
                separator=" ",
                strip=True
            ) or "Dokument PDF"

            linki.append({
                "nazwa": nazwa_pliku,
                "url": pelny_url
            })

            print(f"  ✓ {nazwa_pliku}")
            print(f"    {pelny_url}")

    print(f"\nŁącznie wykryto {len(linki)} plików PDF.")
    return linki
